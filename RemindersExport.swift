#!/usr/bin/env swift

import EventKit
import Foundation

struct Options {
    var lookaheadDays = 365
    var listNames = Set<String>()
    var includeUndated = false
    var completedOnly = false
    var listsOnly = false
    var showHelp = false
}

func printUsage() {
    let text = """
    Usage: swift RemindersExport.swift [options]

    Options:
      --lookahead-days DAYS   Export dated incomplete reminders due within this many days. Default: 365
      --include-undated       Also export incomplete reminders without a due date or alarm.
      --completed-only        Export completed reminders instead of incomplete reminders.
      --list NAME             Limit to a Reminders list. May be repeated.
      --lists-only            Export Reminders lists instead of reminders.
      --help                  Show this help.
    """
    FileHandle.standardError.write(Data(text.utf8))
}

func fail(_ message: String, code: Int32 = 1) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(code)
}

func parseOptions(_ args: [String]) -> Options {
    var options = Options()
    var index = 1

    while index < args.count {
        let arg = args[index]
        switch arg {
        case "--help", "-h":
            options.showHelp = true
            index += 1
        case "--lookahead-days":
            guard index + 1 < args.count, let value = Int(args[index + 1]), value >= 0 else {
                fail("Invalid value for --lookahead-days")
            }
            options.lookaheadDays = value
            index += 2
        case "--include-undated":
            options.includeUndated = true
            index += 1
        case "--completed-only":
            options.completedOnly = true
            index += 1
        case "--list":
            guard index + 1 < args.count else {
                fail("Missing value for --list")
            }
            options.listNames.insert(args[index + 1])
            index += 2
        case "--lists-only":
            options.listsOnly = true
            index += 1
        default:
            fail("Unknown argument: \(arg)")
        }
    }

    return options
}

func isoString(_ date: Date?) -> String? {
    guard let date else {
        return nil
    }

    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime]
    formatter.timeZone = TimeZone(secondsFromGMT: 0)
    return formatter.string(from: date)
}

func jsonValue(_ value: String?) -> Any {
    value ?? NSNull()
}

func dateString(from components: DateComponents?) -> String? {
    guard
        let components,
        let year = components.year,
        let month = components.month,
        let day = components.day
    else {
        return nil
    }

    return String(format: "%04d-%02d-%02d", year, month, day)
}

func dateFromComponents(_ components: DateComponents?) -> Date? {
    guard var components else {
        return nil
    }

    var calendar = components.calendar ?? Calendar.current
    if let timeZone = components.timeZone {
        calendar.timeZone = timeZone
    }
    components.calendar = calendar
    return calendar.date(from: components)
}

func isAllDay(_ components: DateComponents?) -> Bool {
    guard let components else {
        return false
    }

    return components.hour == nil && components.minute == nil && components.second == nil
}

func requestReminderAccess(_ store: EKEventStore) -> Bool {
    let semaphore = DispatchSemaphore(value: 0)
    var granted = false
    var requestError: Error?

    if #available(macOS 14.0, *) {
        store.requestFullAccessToReminders { accessGranted, error in
            granted = accessGranted
            requestError = error
            semaphore.signal()
        }
    } else {
        store.requestAccess(to: .reminder) { accessGranted, error in
            granted = accessGranted
            requestError = error
            semaphore.signal()
        }
    }

    _ = semaphore.wait(timeout: .now() + 120)

    if let requestError {
        fail("Reminders permission error: \(requestError.localizedDescription)")
    }

    return granted
}

func fetchIncompleteReminders(store: EKEventStore, calendars: [EKCalendar]) -> [EKReminder] {
    let semaphore = DispatchSemaphore(value: 0)
    let predicate = store.predicateForIncompleteReminders(withDueDateStarting: nil, ending: nil, calendars: calendars)
    var fetched: [EKReminder] = []

    store.fetchReminders(matching: predicate) { reminders in
        fetched = reminders ?? []
        semaphore.signal()
    }

    _ = semaphore.wait(timeout: .now() + 120)
    return fetched
}

func fetchCompletedReminders(store: EKEventStore, calendars: [EKCalendar]) -> [EKReminder] {
    let semaphore = DispatchSemaphore(value: 0)
    let predicate = store.predicateForCompletedReminders(withCompletionDateStarting: nil, ending: nil, calendars: calendars)
    var fetched: [EKReminder] = []

    store.fetchReminders(matching: predicate) { reminders in
        fetched = reminders ?? []
        semaphore.signal()
    }

    _ = semaphore.wait(timeout: .now() + 120)
    return fetched
}

let options = parseOptions(CommandLine.arguments)

if options.showHelp {
    printUsage()
    exit(0)
}

let store = EKEventStore()
guard requestReminderAccess(store) else {
    fail("Reminders access was denied. Enable it in System Settings > Privacy & Security > Reminders.")
}

let allCalendars = store.calendars(for: .reminder)
let selectedCalendars = allCalendars.filter { calendar in
    options.listNames.isEmpty || options.listNames.contains(calendar.title)
}

if !options.listNames.isEmpty && selectedCalendars.isEmpty {
    fail("No Reminders lists matched: \(Array(options.listNames).sorted().joined(separator: ", "))")
}

if options.listsOnly {
    let exportedLists = selectedCalendars.map { calendar -> [String: Any] in
        let source = calendar.source
        return [
            "id": calendar.calendarIdentifier,
            "title": calendar.title,
            "account_title": source?.title ?? "",
            "account_id": source?.sourceIdentifier ?? ""
        ]
    }

    do {
        let data = try JSONSerialization.data(withJSONObject: exportedLists, options: [.prettyPrinted, .sortedKeys])
        FileHandle.standardOutput.write(data)
        FileHandle.standardOutput.write(Data("\n".utf8))
    } catch {
        fail("Failed to encode reminder lists as JSON: \(error.localizedDescription)")
    }
    exit(0)
}

let now = Date()
let latestDate = Calendar.current.date(byAdding: .day, value: options.lookaheadDays, to: now) ?? now
let reminders = options.completedOnly
    ? fetchCompletedReminders(store: store, calendars: selectedCalendars)
    : fetchIncompleteReminders(store: store, calendars: selectedCalendars)
let exported = reminders.compactMap { reminder -> [String: Any]? in
    let dueComponents = reminder.dueDateComponents
    let allDay = isAllDay(dueComponents)
    let dueDate = dateFromComponents(dueComponents)
    let alarmDate = reminder.alarms?.compactMap { $0.absoluteDate }.sorted().first
    let chosenDate = dueDate ?? alarmDate

    if !options.completedOnly && chosenDate == nil && !options.includeUndated {
        return nil
    }

    if !options.completedOnly, let chosenDate, chosenDate > latestDate {
        return nil
    }

    let calendar = reminder.calendar
    let source = calendar?.source
    let recurrenceRules = reminder.recurrenceRules ?? []
    let itemIdentifier = reminder.calendarItemIdentifier
    let externalIdentifier = reminder.calendarItemExternalIdentifier ?? ""
    let stableID = externalIdentifier.isEmpty ? itemIdentifier : externalIdentifier

    return [
        "id": itemIdentifier,
        "external_id": externalIdentifier,
        "stable_id": stableID,
        "title": reminder.title ?? "",
        "notes": reminder.notes ?? "",
        "list_title": calendar?.title ?? "",
        "list_id": calendar?.calendarIdentifier ?? "",
        "account_title": source?.title ?? "",
        "account_id": source?.sourceIdentifier ?? "",
        "priority": reminder.priority,
        "is_completed": reminder.isCompleted,
        "is_recurring": !recurrenceRules.isEmpty,
        "recurrence_count": recurrenceRules.count,
        "created_at": jsonValue(isoString(reminder.creationDate)),
        "modified_at": jsonValue(isoString(reminder.lastModifiedDate)),
        "completed_at": jsonValue(isoString(reminder.completionDate)),
        "due_at": jsonValue(isoString(chosenDate)),
        "due_date": chosenDate != nil && allDay ? jsonValue(dateString(from: dueComponents)) : NSNull(),
        "all_day": chosenDate != nil && allDay,
        "date_source": chosenDate == nil ? "none" : (dueDate == nil && alarmDate != nil ? "alarm" : "due")
    ]
}

do {
    let data = try JSONSerialization.data(withJSONObject: exported, options: [.prettyPrinted, .sortedKeys])
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data("\n".utf8))
} catch {
    fail("Failed to encode reminders as JSON: \(error.localizedDescription)")
}
