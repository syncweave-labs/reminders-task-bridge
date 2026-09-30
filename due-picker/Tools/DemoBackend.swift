// DemoBackend.swift — in-memory reminders for tests and screenshots. Never
// touches EventKit data; not part of the app bundle.

import Foundation

@MainActor
final class DemoBackend: ReminderBackend {
    let calendar: Calendar
    var onStoreChange: (() -> Void)?
    var access: AccessState = .granted
    var lists: [ReminderList]
    var items: [ReminderItem]
    /// Makes the next `apply` fail as a whole, like a refused EventKit commit.
    var failNextCommit = false
    private var createdCount = 0

    init(now: Date, calendar: Calendar) {
        self.calendar = calendar
        let today = Day(now, calendar: calendar)
        func day(_ offset: Int) -> Day { today.adding(days: offset, calendar: calendar) }

        lists = [
            ReminderList(id: "todo", title: "할 일", accountTitle: "iCloud",
                         color: RGBColor(red: 0.04, green: 0.52, blue: 1.0), isEditable: true),
            ReminderList(id: "school", title: "학교", accountTitle: "iCloud",
                         color: RGBColor(red: 1.0, green: 0.58, blue: 0.0), isEditable: true),
            ReminderList(id: "personal", title: "개인", accountTitle: "iCloud",
                         color: RGBColor(red: 0.2, green: 0.78, blue: 0.35), isEditable: true),
            ReminderList(id: "club", title: "동아리 (공유)", accountTitle: "iCloud",
                         color: RGBColor(red: 0.69, green: 0.32, blue: 0.87), isEditable: false),
        ]

        func item(_ id: String, _ title: String, _ list: String, _ due: DueValue?,
                  recurring: Bool = false, alarms: Bool = false, editable: Bool = true) -> ReminderItem {
            ReminderItem(id: id, title: title, listID: list, due: due, hasUnreadableDue: false,
                         isRecurring: recurring, isEditable: editable, hasAlarms: alarms || due?.time != nil,
                         hasNotes: false)
        }

        items = [
            item("r-assignment", "자료구조 과제 3 제출", "school", DueValue(day(-1))),
            item("r-quiz", "운영체제 퀴즈 준비", "school", DueValue(day(0), TimeOfDay(21, 0))),
            item("r-library", "도서관 책 반납", "personal", DueValue(day(0))),
            item("r-scholarship", "장학금 서류 스캔해서 올리기", "todo", DueValue(day(1), TimeOfDay(10, 0))),
            item("r-meeting", "팀 프로젝트 회의", "school", DueValue(day(2), TimeOfDay(15, 0)), recurring: true),
            item("r-dues", "동아리 회비 정산", "club", DueValue(day(3)), editable: false),
            item("r-gym", "헬스장 재등록", "personal", DueValue(day(5))),
            item("r-study", "알고리즘 스터디 발표 자료", "school", DueValue(day(9))),
            item("r-backup", "노트북 백업하기", "todo", nil),
            item("r-trip", "겨울 여행 숙소 알아보기", "personal", nil),
            item("r-unsubscribe", "안 쓰는 구독 해지", "todo", nil),
        ]
    }

    func accessState() -> AccessState { access }

    func requestAccess() async -> AccessState { access }

    func fetchLists() -> [ReminderList] { lists }

    func fetchReminders() async -> [ReminderItem] { items }

    func defaultListID() -> String? { lists.first?.id }

    func apply(_ requests: [DueChangeRequest]) -> ApplyReport {
        var report = ApplyReport()
        var previous: [(id: String, due: DueValue?, result: DueValue?)] = []
        for request in requests {
            guard let index = items.firstIndex(where: { $0.id == request.id }) else {
                report.skipped.append((request.id, .missing))
                continue
            }
            guard items[index].due == request.expected else {
                report.skipped.append((request.id, .changedElsewhere))
                continue
            }
            if !items[index].isEditable {
                report.skipped.append((request.id, .blocked(.readOnlyList)))
                continue
            }
            if request.new == nil && items[index].isRecurring {
                report.skipped.append((request.id, .blocked(.recurringNeedsDate)))
                continue
            }
            previous.append((request.id, items[index].due, request.new))
            items[index].due = request.new
            items[index].hasUnreadableDue = false
        }
        if failNextCommit {
            failNextCommit = false
            for entry in previous {
                if let index = items.firstIndex(where: { $0.id == entry.id }) { items[index].due = entry.due }
            }
            report.failure = "demo commit refused"
            return report
        }
        report.changed = previous.map(\.id)
        if !previous.isEmpty {
            report.undo = UndoAction { [weak self] in
                guard let self else { return ApplyReport(failure: "gone") }
                var undone = ApplyReport()
                for entry in previous {
                    guard let index = self.items.firstIndex(where: { $0.id == entry.id }) else {
                        undone.skipped.append((entry.id, .missing))
                        continue
                    }
                    guard self.items[index].due == entry.result else {
                        undone.skipped.append((entry.id, .changedElsewhere))
                        continue
                    }
                    self.items[index].due = entry.due
                    undone.changed.append(entry.id)
                    undone.resultingDues[entry.id] = .some(entry.due)
                }
                return undone
            }
        }
        return report
    }

    func create(title: String, listID: String?, due: DueValue?) throws -> String {
        guard let list = lists.first(where: { $0.id == (listID ?? defaultListID()) }) else {
            throw ReminderStoreError.noList
        }
        guard list.isEditable else { throw ReminderStoreError.readOnlyList }
        createdCount += 1
        let id = "new-\(createdCount)"
        items.append(ReminderItem(id: id, title: title, listID: list.id, due: due, hasUnreadableDue: false,
                                  isRecurring: false, isEditable: true, hasAlarms: due?.time != nil, hasNotes: false))
        return id
    }
}
