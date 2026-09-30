// ReminderStore.swift — reads and writes Apple Reminders through EventKit.
//
// Only a reminder's due date, the start date that mirrors it, and the clock
// alarms anchored to it are ever changed; title, notes, list, priority,
// recurrence and every other alarm stay as they are.

import CoreGraphics
import EventKit
import Foundation

struct RGBColor: Hashable {
    var red: Double
    var green: Double
    var blue: Double

    static let fallback = RGBColor(red: 0.56, green: 0.56, blue: 0.58)

    init(red: Double, green: Double, blue: Double) {
        self.red = red
        self.green = green
        self.blue = blue
    }

    init(_ color: CGColor?) {
        guard let color, let space = CGColorSpace(name: CGColorSpace.sRGB),
              let converted = color.converted(to: space, intent: .defaultIntent, options: nil),
              let parts = converted.components, parts.count >= 3 else {
            self = .fallback
            return
        }
        self.init(red: Double(parts[0]), green: Double(parts[1]), blue: Double(parts[2]))
    }
}

struct ReminderList: Identifiable, Hashable {
    let id: String
    let title: String
    let accountTitle: String
    let color: RGBColor
    let isEditable: Bool
}

struct ReminderItem: Identifiable, Hashable {
    let id: String
    var title: String
    var listID: String
    var due: DueValue?
    /// Due components exist but cannot be read; only an explicit day or "no date" replaces them.
    var hasUnreadableDue: Bool
    var isRecurring: Bool
    var isEditable: Bool
    var hasAlarms: Bool
    var hasNotes: Bool

    var displayTitle: String { title.isEmpty ? "(제목 없음)" : title }
}

enum AccessState: Equatable {
    case checking
    case notDetermined
    case granted
    case denied
    case restricted
    case failed(String)
}

struct DueChangeRequest: Hashable {
    let id: String
    /// The due value the edit was computed from. A reminder that no longer has
    /// it was changed somewhere else in the meantime and is left alone.
    let expected: DueValue?
    let new: DueValue?
}

enum SkipReason: Hashable {
    case missing
    case changedElsewhere
    case blocked(EditBlock)
    case failed(String)

    var message: String {
        switch self {
        case .missing: return "이미 완료되었거나 삭제된 미리알림이에요"
        case .changedElsewhere: return "다른 곳에서 먼저 바뀌어 그대로 두었어요"
        case .blocked(let reason): return reason.message
        case .failed(let message): return "저장하지 못했어요 (\(message))"
        }
    }
}

struct ApplyReport {
    var changed: [String] = []
    var skipped: [(id: String, reason: SkipReason)] = []
    /// Nothing was saved because the store refused the whole commit.
    var failure: String?
    var undo: UndoAction?
    /// The due value each changed reminder has now (filled by undo).
    var resultingDues: [String: DueValue?] = [:]
}

@MainActor
final class UndoAction {
    private let body: () -> ApplyReport

    init(_ body: @escaping () -> ApplyReport) { self.body = body }

    func run() -> ApplyReport { body() }
}

enum ReminderStoreError: LocalizedError {
    case noList
    case readOnlyList

    var errorDescription: String? {
        switch self {
        case .noList: return "미리알림을 넣을 목록이 없어요"
        case .readOnlyList: return "읽기 전용 목록에는 추가할 수 없어요"
        }
    }
}

@MainActor
protocol ReminderBackend: AnyObject {
    var calendar: Calendar { get }
    /// Called on the main actor whenever the underlying store changed.
    var onStoreChange: (() -> Void)? { get set }
    func accessState() -> AccessState
    func requestAccess() async -> AccessState
    func fetchLists() -> [ReminderList]
    func fetchReminders() async -> [ReminderItem]
    func defaultListID() -> String?
    func apply(_ requests: [DueChangeRequest]) -> ApplyReport
    func create(title: String, listID: String?, due: DueValue?) throws -> String
}

// MARK: - Moving one reminder (pure EventKit object edits, no saving)

/// What one date change did to a reminder: enough to put it back exactly.
struct DueChangeRecord {
    let previousDue: DateComponents?
    let previousStart: DateComponents?
    let removedAlarms: [EKAlarm]
    let addedAlarmDates: [Date]
    let resultingDue: DueValue?
}

private func isClockAlarm(_ alarm: EKAlarm) -> Bool {
    alarm.absoluteDate != nil && alarm.structuredLocation == nil
}

/// Moves a reminder's due date in memory; the caller saves. Clock alarms
/// anchored to the old due date and a start date that mirrored it follow the
/// move (see `planAlarms` and `planStart`). EventKit itself fills an empty
/// start with the new due date, as Reminders does.
func moveDue(of reminder: EKReminder, to newDue: DueValue?, calendar: Calendar) -> DueChangeRecord {
    let previousDue = reminder.dueDateComponents
    let previousStart = reminder.startDateComponents
    let oldDue = DueValue(components: previousDue, calendar: calendar)
    let clockAlarms = (reminder.alarms ?? []).filter(isClockAlarm)
    let alarmPlan = planAlarms(oldDue: oldDue, newDue: newDue,
                               absoluteAlarms: clockAlarms.compactMap(\.absoluteDate), calendar: calendar)

    reminder.dueDateComponents = newDue?.components(calendar: calendar, previous: previousDue)

    switch planStart(start: DueValue(components: previousStart, calendar: calendar),
                     oldDue: oldDue, newDue: newDue, calendar: calendar) {
    case .keep:
        break
    case .clear:
        reminder.startDateComponents = nil
    case .set(let value):
        reminder.startDateComponents = startComponents(for: value, previous: previousStart, calendar: calendar)
    }

    var removed: [EKAlarm] = []
    for index in alarmPlan.remove where clockAlarms.indices.contains(index) {
        let alarm = clockAlarms[index]
        if let copy = alarm.copy() as? EKAlarm { removed.append(copy) }
        reminder.removeAlarm(alarm)
    }
    for date in alarmPlan.add {
        reminder.addAlarm(EKAlarm(absoluteDate: date))
    }
    return DueChangeRecord(previousDue: previousDue, previousStart: previousStart,
                           removedAlarms: removed, addedAlarmDates: alarmPlan.add, resultingDue: newDue)
}

/// Reverses `moveDue` in memory; the caller saves. The start is written after
/// the due date because EventKit fills an empty start when a due date is set.
func restoreDue(of reminder: EKReminder, from record: DueChangeRecord) {
    reminder.dueDateComponents = record.previousDue
    reminder.startDateComponents = record.previousStart
    for date in record.addedAlarmDates {
        let match = (reminder.alarms ?? []).first { alarm in
            guard isClockAlarm(alarm), let when = alarm.absoluteDate else { return false }
            return abs(when.timeIntervalSince(date)) < alarmMatchTolerance
        }
        if let match { reminder.removeAlarm(match) }
    }
    for alarm in record.removedAlarms {
        if let copy = alarm.copy() as? EKAlarm { reminder.addAlarm(copy) }
    }
}

// MARK: - EventKit backend

@MainActor
final class EventKitBackend: ReminderBackend {
    private let store = EKEventStore()
    private var remindersByID: [String: EKReminder] = [:]
    private var changeObserver: NSObjectProtocol?
    var onStoreChange: (() -> Void)?

    var calendar: Calendar { Calendar.autoupdatingCurrent }

    init() {
        changeObserver = NotificationCenter.default.addObserver(
            forName: .EKEventStoreChanged, object: store, queue: .main
        ) { [weak self] _ in
            Task { @MainActor in self?.onStoreChange?() }
        }
    }

    func accessState() -> AccessState {
        switch EKEventStore.authorizationStatus(for: .reminder) {
        case .fullAccess: return .granted
        case .notDetermined: return .notDetermined
        case .restricted: return .restricted
        case .denied, .writeOnly: return .denied
        @unknown default: return .denied
        }
    }

    func requestAccess() async -> AccessState {
        do {
            let granted = try await store.requestFullAccessToReminders()
            if granted { store.reset() }
            return granted ? .granted : accessState()
        } catch {
            return .failed(error.localizedDescription)
        }
    }

    func fetchLists() -> [ReminderList] {
        store.calendars(for: .reminder)
            .map { list in
                ReminderList(id: list.calendarIdentifier, title: list.title,
                             accountTitle: list.source?.title ?? "", color: RGBColor(list.cgColor),
                             isEditable: list.allowsContentModifications)
            }
            .sorted { lhs, rhs in
                let byTitle = lhs.title.localizedStandardCompare(rhs.title)
                return byTitle == .orderedSame ? lhs.id < rhs.id : byTitle == .orderedAscending
            }
    }

    func fetchReminders() async -> [ReminderItem] {
        let predicate = store.predicateForIncompleteReminders(withDueDateStarting: nil, ending: nil, calendars: nil)
        let fetched: [EKReminder] = await withCheckedContinuation { continuation in
            store.fetchReminders(matching: predicate) { reminders in
                continuation.resume(returning: reminders ?? [])
            }
        }
        let calendar = self.calendar
        var byID: [String: EKReminder] = [:]
        var items: [ReminderItem] = []
        for reminder in fetched where !reminder.isCompleted {
            let id = reminder.calendarItemIdentifier
            guard !id.isEmpty, byID[id] == nil else { continue }
            byID[id] = reminder
            let due = DueValue(components: reminder.dueDateComponents, calendar: calendar)
            items.append(ReminderItem(
                id: id,
                title: reminder.title ?? "",
                listID: reminder.calendar?.calendarIdentifier ?? "",
                due: due,
                hasUnreadableDue: reminder.dueDateComponents != nil && due == nil,
                isRecurring: reminder.hasRecurrenceRules,
                isEditable: reminder.calendar?.allowsContentModifications ?? false,
                hasAlarms: reminder.hasAlarms,
                hasNotes: reminder.hasNotes
            ))
        }
        remindersByID = byID
        return items
    }

    func defaultListID() -> String? {
        store.defaultCalendarForNewReminders()?.calendarIdentifier
    }

    /// The latest stored version of a reminder, or nil once it is gone.
    private func lookup(_ id: String) -> EKReminder? {
        if let cached = remindersByID[id], cached.refresh() { return cached }
        guard let fresh = store.calendarItem(withIdentifier: id) as? EKReminder else { return nil }
        remindersByID[id] = fresh
        return fresh
    }

    func apply(_ requests: [DueChangeRequest]) -> ApplyReport {
        var report = ApplyReport()
        var saved: [(id: String, record: DueChangeRecord)] = []
        let calendar = self.calendar
        for request in requests {
            guard let reminder = lookup(request.id), !reminder.isCompleted else {
                report.skipped.append((request.id, .missing))
                continue
            }
            let current = DueValue(components: reminder.dueDateComponents, calendar: calendar)
            guard current == request.expected else {
                report.skipped.append((request.id, .changedElsewhere))
                continue
            }
            if reminder.calendar?.allowsContentModifications == false {
                report.skipped.append((request.id, .blocked(.readOnlyList)))
                continue
            }
            if request.new == nil, reminder.hasRecurrenceRules {
                report.skipped.append((request.id, .blocked(.recurringNeedsDate)))
                continue
            }
            let unreadable = reminder.dueDateComponents != nil && current == nil
            guard current != request.new || unreadable else { continue }

            let record = moveDue(of: reminder, to: request.new, calendar: calendar)
            do {
                try store.save(reminder, commit: false)
                saved.append((request.id, record))
            } catch {
                reminder.rollback()
                report.skipped.append((request.id, .failed(error.localizedDescription)))
            }
        }
        guard !saved.isEmpty else { return report }
        do {
            try store.commit()
        } catch {
            store.reset()
            remindersByID = [:]
            report.failure = error.localizedDescription
            return report
        }
        report.changed = saved.map(\.id)
        report.undo = UndoAction { [weak self] in
            guard let self else { return ApplyReport(failure: "앱 상태가 바뀌어 되돌릴 수 없어요") }
            return self.restore(saved)
        }
        return report
    }

    private func restore(_ entries: [(id: String, record: DueChangeRecord)]) -> ApplyReport {
        var report = ApplyReport()
        var restored: [String] = []
        let calendar = self.calendar
        for entry in entries {
            guard let reminder = lookup(entry.id) else {
                report.skipped.append((entry.id, .missing))
                continue
            }
            guard DueValue(components: reminder.dueDateComponents, calendar: calendar) == entry.record.resultingDue else {
                report.skipped.append((entry.id, .changedElsewhere))
                continue
            }
            restoreDue(of: reminder, from: entry.record)
            do {
                try store.save(reminder, commit: false)
                restored.append(entry.id)
                report.resultingDues[entry.id] = .some(DueValue(components: entry.record.previousDue, calendar: calendar))
            } catch {
                reminder.rollback()
                report.skipped.append((entry.id, .failed(error.localizedDescription)))
            }
        }
        guard !restored.isEmpty else { return report }
        do {
            try store.commit()
        } catch {
            store.reset()
            remindersByID = [:]
            report.failure = error.localizedDescription
            report.resultingDues = [:]
            return report
        }
        report.changed = restored
        return report
    }

    func create(title: String, listID: String?, due: DueValue?) throws -> String {
        let target = listID.flatMap { store.calendar(withIdentifier: $0) } ?? store.defaultCalendarForNewReminders()
        guard let target else { throw ReminderStoreError.noList }
        guard target.allowsContentModifications else { throw ReminderStoreError.readOnlyList }
        let reminder = EKReminder(eventStore: store)
        reminder.title = title
        reminder.calendar = target
        if let due {
            // Same rules as a move from "no date": a timed reminder gets its alarm.
            _ = moveDue(of: reminder, to: due, calendar: calendar)
        }
        try store.save(reminder, commit: true)
        remindersByID[reminder.calendarItemIdentifier] = reminder
        return reminder.calendarItemIdentifier
    }
}
