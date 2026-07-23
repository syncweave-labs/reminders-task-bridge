#!/usr/bin/env swift

import EventKit
import Foundation

func fail(_ message: String, code: Int32 = 1) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(code)
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

func fetchAllReminders(store: EKEventStore, calendars: [EKCalendar]) -> [EKReminder] {
    let semaphore = DispatchSemaphore(value: 0)
    let predicate = store.predicateForReminders(in: calendars)
    var fetched: [EKReminder] = []

    store.fetchReminders(matching: predicate) { reminders in
        fetched = reminders ?? []
        semaphore.signal()
    }

    _ = semaphore.wait(timeout: .now() + 120)
    return fetched
}

func normalizeIdentifier(_ value: String?) -> String {
    guard let value else {
        return ""
    }
    let allowed = Set("0123456789abcdef")
    return String(value.lowercased().filter { allowed.contains($0) })
}

func parseISODate(_ value: String) -> Date? {
    let fractional = ISO8601DateFormatter()
    fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    if let date = fractional.date(from: value) {
        return date
    }

    let plain = ISO8601DateFormatter()
    plain.formatOptions = [.withInternetDateTime]
    return plain.date(from: value)
}

func isoString(_ date: Date?) -> String {
    guard let date else {
        return ""
    }

    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime]
    formatter.timeZone = TimeZone(secondsFromGMT: 0)
    return formatter.string(from: date)
}

func dateComponents(from dateText: String) -> DateComponents? {
    let parts = dateText.split(separator: "-")
    guard
        parts.count == 3,
        let year = Int(parts[0]),
        let month = Int(parts[1]),
        let day = Int(parts[2])
    else {
        return nil
    }

    var components = DateComponents()
    components.calendar = Calendar.current
    components.year = year
    components.month = month
    components.day = day
    return components
}

func dateTimeComponents(from dateText: String) -> DateComponents? {
    guard let date = parseISODate(dateText) else {
        return nil
    }

    var components = Calendar.current.dateComponents([.year, .month, .day, .hour, .minute, .second], from: date)
    components.calendar = Calendar.current
    components.timeZone = TimeZone.current
    return components
}

func applyFields(to reminder: EKReminder, operation: [String: Any]) {
    if let title = operation["title"] as? String {
        reminder.title = title
    }

    if operation.keys.contains("notes") {
        reminder.notes = (operation["notes"] as? String) ?? ""
    }

    if operation["clear_due"] as? Bool == true {
        reminder.dueDateComponents = nil
    }

    if let allDay = operation["all_day"] as? Bool {
        if allDay, let dueDate = operation["due_date"] as? String, let components = dateComponents(from: dueDate) {
            reminder.dueDateComponents = components
        } else if !allDay, let dueAt = operation["due_at"] as? String, let components = dateTimeComponents(from: dueAt) {
            reminder.dueDateComponents = components
        }
    }

    if operation["complete"] as? Bool == true {
        reminder.isCompleted = true
        reminder.completionDate = Date()
    }
}

func result(for reminder: EKReminder, status: String, stableID requestedStableID: String = "") -> [String: Any] {
    let itemIdentifier = reminder.calendarItemIdentifier ?? ""
    let externalIdentifier = reminder.calendarItemExternalIdentifier ?? ""
    let stableID = !externalIdentifier.isEmpty ? externalIdentifier : (!itemIdentifier.isEmpty ? itemIdentifier : requestedStableID)
    let calendar = reminder.calendar
    let source = calendar?.source

    return [
        "stable_id": stableID,
        "id": itemIdentifier,
        "external_id": externalIdentifier,
        "status": status,
        "list_title": calendar?.title ?? "",
        "list_id": calendar?.calendarIdentifier ?? "",
        "account_title": source?.title ?? "",
        "account_id": source?.sourceIdentifier ?? "",
        "created_at": isoString(reminder.creationDate),
        "modified_at": isoString(reminder.lastModifiedDate)
    ]
}

let inputData = FileHandle.standardInput.readDataToEndOfFile()
guard !inputData.isEmpty else {
    fail("Expected JSON operations on stdin.")
}

let rawPayload: Any
do {
    rawPayload = try JSONSerialization.jsonObject(with: inputData)
} catch {
    fail("Invalid JSON input: \(error.localizedDescription)")
}

guard let operations = rawPayload as? [[String: Any]] else {
    fail("Expected a JSON array of operation objects.")
}

let store = EKEventStore()
guard requestReminderAccess(store) else {
    fail("Reminders access was denied. Enable it in System Settings > Privacy & Security > Reminders.")
}

let calendars = store.calendars(for: .reminder)
var calendarsByIdentifier: [String: EKCalendar] = [:]
var calendarsByTitle: [String: [EKCalendar]] = [:]
for calendar in calendars {
    let identifier = calendar.calendarIdentifier.trimmingCharacters(in: .whitespacesAndNewlines)
    if !identifier.isEmpty {
        calendarsByIdentifier[identifier] = calendar
    }
    calendarsByTitle[calendar.title, default: []].append(calendar)
}

let reminders = fetchAllReminders(store: store, calendars: calendars)
var remindersByIdentifier: [String: EKReminder] = [:]

for reminder in reminders {
    let identifiers = [
        reminder.calendarItemIdentifier,
        reminder.calendarItemExternalIdentifier
    ]

    for identifier in identifiers {
        let normalized = normalizeIdentifier(identifier)
        if !normalized.isEmpty {
            remindersByIdentifier[normalized] = reminder
        }
    }
}

var results: [[String: Any]] = []

for operation in operations {
    if operation["create"] as? Bool == true {
        let listID = ((operation["list_id"] as? String) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        let listTitle = ((operation["list_title"] as? String) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        let titleMatches = listTitle.isEmpty ? [] : (calendarsByTitle[listTitle] ?? [])
        let targetCalendar: EKCalendar?
        let missingReason: String

        if !listID.isEmpty {
            // A stable identifier is authoritative. Falling through to a title after
            // an identifier miss could write into a different list with the same name.
            targetCalendar = calendarsByIdentifier[listID]
            missingReason = "list_id_not_found"
        } else if listTitle.isEmpty {
            targetCalendar = calendars.first
            missingReason = "no_reminder_lists"
        } else if titleMatches.count == 1 {
            // Backward compatibility for older producers that only sent list_title.
            targetCalendar = titleMatches.first
            missingReason = ""
        } else {
            targetCalendar = nil
            missingReason = titleMatches.isEmpty ? "list_title_not_found" : "ambiguous_list_title"
        }

        guard let calendar = targetCalendar else {
            results.append([
                "list_id": listID,
                "list_title": listTitle,
                "status": "missing_list",
                "reason": missingReason
            ])
            continue
        }

        let reminder = EKReminder(eventStore: store)
        reminder.calendar = calendar
        applyFields(to: reminder, operation: operation)

        do {
            try store.save(reminder, commit: true)
            results.append(result(for: reminder, status: "created"))
        } catch {
            results.append(["list_title": listTitle, "status": "error", "message": error.localizedDescription])
        }
        continue
    }

    let stableID = (operation["stable_id"] as? String) ?? (operation["id"] as? String) ?? ""
    let normalizedID = normalizeIdentifier(stableID)
    guard let reminder = remindersByIdentifier[normalizedID] else {
        results.append(["stable_id": stableID, "status": "missing"])
        continue
    }

    do {
        if operation["delete"] as? Bool == true {
            try store.remove(reminder, commit: false)
            results.append(["stable_id": stableID, "status": "deleted"])
            continue
        }

        applyFields(to: reminder, operation: operation)
        try store.save(reminder, commit: false)
        results.append(result(for: reminder, status: "updated", stableID: stableID))
    } catch {
        results.append(["stable_id": stableID, "status": "error", "message": error.localizedDescription])
    }
}

do {
    try store.commit()
    let output = try JSONSerialization.data(withJSONObject: results, options: [.prettyPrinted, .sortedKeys])
    FileHandle.standardOutput.write(output)
    FileHandle.standardOutput.write(Data("\n".utf8))
} catch {
    fail("Failed to commit Reminders changes: \(error.localizedDescription)")
}
