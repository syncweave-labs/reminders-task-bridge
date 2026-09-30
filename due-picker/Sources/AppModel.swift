// AppModel.swift — state and actions behind the window.

import AppKit
import Combine
import Foundation

enum ReminderFilter: Hashable {
    case all, today, overdue, scheduled, undated
    case list(String)
}

enum FocusTarget: Hashable {
    case dateInput, quickAddTitle
}

struct StatusMessage: Equatable {
    enum Tone { case info, success, warning, error }

    let tone: Tone
    let text: String
    /// Offers "되돌리기" next to the message.
    let offersUndo: Bool

    init(_ tone: Tone, _ text: String, offersUndo: Bool = false) {
        self.tone = tone
        self.text = text
        self.offersUndo = offersUndo
    }
}

struct ReminderSection: Identifiable {
    let bucket: DueBucket
    let items: [ReminderItem]

    var id: Int { bucket.rawValue }
}

@MainActor
final class AppModel: ObservableObject {
    @Published private(set) var access: AccessState = .checking
    @Published private(set) var lists: [ReminderList] = []
    @Published private(set) var items: [ReminderItem] = []
    @Published private(set) var isLoading = false
    @Published private(set) var loadedOnce = false
    @Published var filter: ReminderFilter = .all
    @Published var search = ""
    @Published var selection: Set<String> = [] {
        didSet { followSelectionInCalendar(previous: oldValue) }
    }
    @Published var dateInput = ""
    @Published var displayedMonth: Day
    @Published var customTime: Date
    @Published var status: StatusMessage?
    @Published var focusRequest: FocusTarget?
    @Published var showsInspector = true
    @Published var quickAddTitle = ""
    @Published var quickAddDate = ""
    @Published var quickAddListID: String?
    /// Re-evaluated every minute so "오늘" and "지연됨" stay right while the app is open.
    @Published private(set) var clock: Date

    let backend: ReminderBackend
    private let nowProvider: () -> Date
    private var undoStack: [(label: String, action: UndoAction)] = []
    @Published private(set) var canUndo = false
    private var reloadTask: Task<Void, Never>?
    private var clockTimer: Timer?

    init(backend: ReminderBackend, now: @escaping () -> Date = Date.init, tickClock: Bool = true) {
        self.backend = backend
        self.nowProvider = now
        let start = now()
        self.clock = start
        let calendar = backend.calendar
        let today = Day(start, calendar: calendar)
        self.displayedMonth = Day(today.year, today.month, 1)
        self.customTime = calendar.date(bySettingHour: 9, minute: 0, second: 0, of: start) ?? start
        backend.onStoreChange = { [weak self] in self?.scheduleReload() }
        if tickClock {
            clockTimer = Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { [weak self] _ in
                Task { @MainActor in self?.tick() }
            }
        }
    }

    var calendar: Calendar { backend.calendar }
    var today: Day { Day(clock, calendar: calendar) }
    var firstWeekday: Int { calendar.firstWeekday }

    private func tick() { clock = nowProvider() }

    // MARK: Loading

    func start() async {
        var state = backend.accessState()
        if state == .notDetermined {
            access = .checking
            state = await backend.requestAccess()
        }
        access = state
        if state == .granted { await reload() }
    }

    func reload() async {
        guard access == .granted else { return }
        isLoading = true
        let fetchedLists = backend.fetchLists()
        let fetched = await backend.fetchReminders()
        lists = fetchedLists
        items = fetched
        let ids = Set(fetched.map(\.id))
        if !selection.isSubset(of: ids) { selection = selection.intersection(ids) }
        if case .list(let id) = filter, !fetchedLists.contains(where: { $0.id == id }) { filter = .all }
        if let chosen = quickAddListID, !fetchedLists.contains(where: { $0.id == chosen }) { quickAddListID = nil }
        clock = nowProvider()
        isLoading = false
        loadedOnce = true
    }

    /// Coalesces the burst of change notifications one save produces.
    func scheduleReload() {
        reloadTask?.cancel()
        reloadTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 300_000_000)
            guard !Task.isCancelled else { return }
            await self?.reload()
        }
    }

    // MARK: Derived state

    func list(for item: ReminderItem) -> ReminderList? { lists.first { $0.id == item.listID } }

    func matches(_ item: ReminderItem, _ filter: ReminderFilter) -> Bool {
        switch filter {
        case .all: return true
        case .today:
            let kind = bucket(for: item.due, now: clock, calendar: calendar)
            return kind == .today || kind == .overdue
        case .overdue: return bucket(for: item.due, now: clock, calendar: calendar) == .overdue
        case .scheduled: return item.due != nil
        case .undated: return item.due == nil
        case .list(let id): return item.listID == id
        }
    }

    func count(for filter: ReminderFilter) -> Int { items.filter { matches($0, filter) }.count }

    var visibleItems: [ReminderItem] {
        let query = search.trimmingCharacters(in: .whitespacesAndNewlines)
        return items.filter { item in
            matches(item, filter) && (query.isEmpty || item.title.localizedCaseInsensitiveContains(query))
        }
    }

    var sections: [ReminderSection] {
        let grouped = Dictionary(grouping: visibleItems) { bucket(for: $0.due, now: clock, calendar: calendar) }
        return DueBucket.allCases.compactMap { kind in
            guard let members = grouped[kind], !members.isEmpty else { return nil }
            return ReminderSection(bucket: kind, items: members.sorted(by: orderedBefore))
        }
    }

    private func orderedBefore(_ lhs: ReminderItem, _ rhs: ReminderItem) -> Bool {
        if let decided = dueSortsBefore(lhs.due, rhs.due) { return decided }
        let byTitle = lhs.title.localizedStandardCompare(rhs.title)
        return byTitle == .orderedSame ? lhs.id < rhs.id : byTitle == .orderedAscending
    }

    var filterTitle: String {
        switch filter {
        case .all: return "전체"
        case .today: return "오늘"
        case .overdue: return "지연됨"
        case .scheduled: return "날짜 있음"
        case .undated: return "날짜 없음"
        case .list(let id): return lists.first { $0.id == id }?.title ?? "목록"
        }
    }

    /// The selected reminders the list is showing. A reminder hidden by the
    /// search or by the current smart list is never changed, even while it
    /// stays selected; it counts again once it is visible.
    var selectedItems: [ReminderItem] {
        visibleItems.filter { selection.contains($0.id) }.sorted(by: orderedBefore)
    }

    var hasSelection: Bool { !selectedItems.isEmpty }

    private var noTargetMessage: String {
        selection.isEmpty ? "먼저 왼쪽 목록에서 미리알림을 고르세요" : "고른 미리알림이 지금 목록에 보이지 않아요"
    }

    /// Incomplete reminders due on each day, across every list.
    var dueCounts: [Day: Int] {
        var counts: [Day: Int] = [:]
        for item in items { if let day = item.due?.day { counts[day, default: 0] += 1 } }
        return counts
    }

    var selectedDays: Set<Day> { Set(selectedItems.compactMap { $0.due?.day }) }

    /// The time every selected reminder shares (`.some(nil)` = all all-day), if they are all dated and share one.
    var sharedSelectedTime: TimeOfDay?? {
        let dues = selectedItems.map(\.due)
        guard let first = dues.first, let firstDue = first,
              dues.allSatisfy({ $0 != nil && $0?.time == firstDue.time }) else { return nil }
        return .some(firstDue.time)
    }

    func label(for item: ReminderItem) -> String {
        item.hasUnreadableDue ? "날짜 확인 필요" : dueLabel(item.due, today: today, calendar: calendar)
    }

    var parsedDateInput: ParseOutcome { parseDueText(dateInput, now: clock, calendar: calendar) }

    /// What pressing Return in the date field would do.
    var dateInputPreview: (text: String, isError: Bool)? {
        switch parsedDateInput {
        case .empty:
            return nil
        case .failure(let message):
            return (message, true)
        case .edit(let edit):
            let targets = selectedItems
            if targets.count == 1, let item = targets.first {
                let result = applyEdit(edit, to: item.due, today: today, calendar: calendar)
                return ("↩︎ " + fullDueLabel(result, today: today, calendar: calendar), false)
            }
            let summary = describeEdit(edit, today: today, calendar: calendar)
            return (targets.isEmpty ? summary : "↩︎ \(targets.count)개 → \(summary)", false)
        }
    }

    // MARK: Changing dates

    func apply(_ edit: DueEdit) {
        let targets = selectedItems
        guard !targets.isEmpty else {
            status = StatusMessage(.warning, noTargetMessage)
            return
        }
        var requests: [DueChangeRequest] = []
        var blocked: [(String, SkipReason)] = []
        var unchanged = 0
        for item in targets {
            if let reason = blockReason(for: edit, isRecurring: item.isRecurring, isEditable: item.isEditable,
                                        hasUnreadableDue: item.hasUnreadableDue) {
                blocked.append((item.id, .blocked(reason)))
                continue
            }
            let new = applyEdit(edit, to: item.due, today: today, calendar: calendar)
            if new == item.due && !item.hasUnreadableDue {
                unchanged += 1
                continue
            }
            requests.append(DueChangeRequest(id: item.id, expected: item.due, new: new))
        }
        guard !requests.isEmpty else {
            if let first = blocked.first {
                status = StatusMessage(.warning, first.1.message)
            } else {
                status = StatusMessage(.info, targets.count == 1 ? "이미 그 날짜예요" : "모두 이미 그 날짜예요")
            }
            return
        }

        let report = backend.apply(requests)
        if let failure = report.failure {
            status = StatusMessage(.error, "저장하지 못했어요: \(failure)")
            scheduleReload()
            return
        }
        let changed = Set(report.changed)
        for request in requests where changed.contains(request.id) {
            if let index = items.firstIndex(where: { $0.id == request.id }) {
                items[index].due = request.new
                items[index].hasUnreadableDue = false
            }
        }

        var summary: String
        if changed.count == 1, let request = requests.first(where: { changed.contains($0.id) }),
           let item = items.first(where: { $0.id == request.id }) {
            summary = "‘\(item.displayTitle)’ → \(fullDueLabel(request.new, today: today, calendar: calendar))"
        } else {
            summary = "\(changed.count)개 → \(describeEdit(edit, today: today, calendar: calendar))"
        }
        if unchanged > 0 { summary += " · \(unchanged)개는 이미 그 날짜" }
        if let undo = report.undo, !changed.isEmpty {
            undoStack.append((summary, undo))
            if undoStack.count > 30 { undoStack.removeFirst(undoStack.count - 30) }
            canUndo = true
        }

        let skipped = blocked + report.skipped.map { ($0.id, $0.reason) }
        if changed.isEmpty {
            status = StatusMessage(.warning, skipped.first?.1.message ?? "바꾸지 못했어요")
        } else if let first = skipped.first {
            status = StatusMessage(.warning, "\(summary) · \(skipped.count)개는 그대로: \(first.1.message)", offersUndo: true)
        } else {
            status = StatusMessage(.success, summary, offersUndo: true)
        }
        if report.skipped.contains(where: { $0.reason == .changedElsewhere || $0.reason == .missing }) {
            scheduleReload()
        }
    }

    func apply(_ preset: DuePreset) { apply(preset.edit(today: today, calendar: calendar)) }

    func pick(day: Day) { apply(DueEdit(day: .set(day))) }

    func pick(time: TimeOfDay?) { apply(DueEdit(time: time.map(TimeChange.set) ?? .clear)) }

    func pickCustomTime() { pick(time: TimeOfDay(customTime, calendar: calendar)) }

    func submitDateInput() {
        switch parsedDateInput {
        case .empty:
            status = StatusMessage(.info, "예: 내일, 금요일 오후 3시, 10/15, 3일 후, 종일, 없음")
        case .failure(let message):
            status = StatusMessage(.error, message)
        case .edit(let edit):
            guard hasSelection else {
                status = StatusMessage(.warning, noTargetMessage)
                return
            }
            apply(edit)
            dateInput = ""
        }
    }

    func undoLast() {
        guard let entry = undoStack.popLast() else {
            status = StatusMessage(.info, "되돌릴 날짜 변경이 없어요")
            return
        }
        canUndo = !undoStack.isEmpty
        let report = entry.action.run()
        for (id, due) in report.resultingDues {
            if let index = items.firstIndex(where: { $0.id == id }) {
                items[index].due = due
                items[index].hasUnreadableDue = false
            }
        }
        if let failure = report.failure {
            status = StatusMessage(.error, "되돌리지 못했어요: \(failure)")
        } else if report.changed.isEmpty {
            status = StatusMessage(.warning, "되돌리지 못했어요: \(report.skipped.first?.reason.message ?? "이미 바뀌었어요")")
        } else if let first = report.skipped.first {
            status = StatusMessage(.warning, "되돌렸어요 · \(report.skipped.count)개는 그대로: \(first.reason.message)")
        } else {
            status = StatusMessage(.success, "되돌렸어요: \(entry.label)")
        }
        scheduleReload()
    }

    /// ⌘Z undoes typing while a text field is being edited, and the last date change otherwise.
    func handleUndoCommand() {
        if let editor = NSApp?.keyWindow?.firstResponder as? NSTextView, editor.isFieldEditor {
            NSApp.sendAction(Selector(("undo:")), to: nil, from: nil)
        } else {
            undoLast()
        }
    }

    private func followSelectionInCalendar(previous: Set<String>) {
        guard selection != previous, selection.count == 1, let id = selection.first,
              let day = items.first(where: { $0.id == id })?.due?.day else { return }
        let month = Day(day.year, day.month, 1)
        if month != displayedMonth { displayedMonth = month }
    }

    func showMonth(offset: Int) { displayedMonth = displayedMonth.adding(months: offset, calendar: calendar) }

    func showCurrentMonth() { displayedMonth = Day(today.year, today.month, 1) }

    // MARK: Quick add

    var quickAddTargetListID: String? {
        if let chosen = quickAddListID { return chosen }
        if case .list(let id) = filter, lists.first(where: { $0.id == id })?.isEditable == true { return id }
        return backend.defaultListID() ?? lists.first(where: \.isEditable)?.id
    }

    var editableLists: [ReminderList] { lists.filter(\.isEditable) }

    /// The due date typed into quick add: nil for none, or an error message.
    var quickAddDue: Result<DueValue?, QuickAddError> {
        switch parseDueText(quickAddDate, now: clock, calendar: calendar) {
        case .empty: return .success(nil)
        case .failure(let message): return .failure(QuickAddError(message: message))
        case .edit(let edit): return .success(applyEdit(edit, to: nil, today: today, calendar: calendar))
        }
    }

    var quickAddPreview: (text: String, isError: Bool)? {
        guard !quickAddDate.trimmingCharacters(in: .whitespaces).isEmpty else { return nil }
        switch quickAddDue {
        case .success(let due): return (fullDueLabel(due, today: today, calendar: calendar), false)
        case .failure(let error): return (error.message, true)
        }
    }

    func submitQuickAdd() { Task { await performQuickAdd() } }

    func performQuickAdd() async {
        let title = quickAddTitle.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !title.isEmpty else {
            focusRequest = .quickAddTitle
            return
        }
        let due: DueValue?
        switch quickAddDue {
        case .success(let value): due = value
        case .failure(let error):
            status = StatusMessage(.error, error.message)
            return
        }
        do {
            let id = try backend.create(title: title, listID: quickAddTargetListID, due: due)
            quickAddTitle = ""
            quickAddDate = ""
            status = StatusMessage(.success, "‘\(title)’ 추가 → \(fullDueLabel(due, today: today, calendar: calendar))")
            await reload()
            if let item = items.first(where: { $0.id == id }) {
                if !matches(item, filter) { filter = .all }
                search = ""
                selection = [id]
            }
        } catch {
            status = StatusMessage(.error, "추가하지 못했어요: \(error.localizedDescription)")
        }
    }
}

struct QuickAddError: Error, Equatable {
    let message: String
}
