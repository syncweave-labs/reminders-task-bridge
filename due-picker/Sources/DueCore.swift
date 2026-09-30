// DueCore.swift — the pure date logic behind 미리알림 날짜.
//
// Foundation only: no EventKit, AppKit or SwiftUI. The same file is compiled
// into the app and into the test runner (scripts/test-due-picker.sh, which CI
// also runs on Linux), so every rule that decides what a date change does to a
// reminder is covered without touching real Reminders data.

import Foundation

private func pad2(_ value: Int) -> String { value < 10 ? "0\(value)" : "\(value)" }

// MARK: - Calendar day and time of day

/// A civil calendar day with no time zone attached.
struct Day: Hashable, Comparable, CustomStringConvertible {
    let year: Int
    let month: Int
    let day: Int

    init(_ year: Int, _ month: Int, _ day: Int) {
        self.year = year
        self.month = month
        self.day = day
    }

    init(_ date: Date, calendar: Calendar) {
        let parts = calendar.dateComponents([.year, .month, .day], from: date)
        self.init(parts.year ?? 1970, parts.month ?? 1, parts.day ?? 1)
    }

    /// The day, only when it exists in the calendar (no 2월 30일).
    static func valid(_ year: Int, _ month: Int, _ day: Int, calendar: Calendar) -> Day? {
        guard (1...9999).contains(year), (1...12).contains(month), day >= 1,
              day <= daysInMonth(year: year, month: month, calendar: calendar) else { return nil }
        return Day(year, month, day)
    }

    static func daysInMonth(year: Int, month: Int, calendar: Calendar) -> Int {
        guard let first = calendar.date(from: DateComponents(year: year, month: month, day: 1, hour: 12)),
              let range = calendar.range(of: .day, in: .month, for: first) else { return 0 }
        return range.count
    }

    static func < (lhs: Day, rhs: Day) -> Bool {
        (lhs.year, lhs.month, lhs.day) < (rhs.year, rhs.month, rhs.day)
    }

    var description: String { "\(year)-\(pad2(month))-\(pad2(day))" }

    /// Day arithmetic runs at noon because no time zone skips or repeats noon.
    func noon(_ calendar: Calendar) -> Date {
        calendar.date(from: DateComponents(year: year, month: month, day: day, hour: 12))
            ?? Date(timeIntervalSince1970: 0)
    }

    func startOfDay(_ calendar: Calendar) -> Date { calendar.startOfDay(for: noon(calendar)) }

    func adding(days count: Int, calendar: Calendar) -> Day {
        guard count != 0, let moved = calendar.date(byAdding: .day, value: count, to: noon(calendar)) else { return self }
        return Day(moved, calendar: calendar)
    }

    /// Adds calendar months and clamps to the month's last day (1월 31일 + 1달 = 2월 28일).
    func adding(months count: Int, calendar: Calendar) -> Day {
        let index = year * 12 + (month - 1) + count
        let newYear = index / 12
        let newMonth = index % 12 + 1
        return Day(newYear, newMonth, min(day, Day.daysInMonth(year: newYear, month: newMonth, calendar: calendar)))
    }

    func lastDayOfMonth(_ calendar: Calendar) -> Day {
        Day(year, month, Day.daysInMonth(year: year, month: month, calendar: calendar))
    }

    /// 1 = Sunday … 7 = Saturday, as in `Calendar`, regardless of the first weekday.
    func weekday(_ calendar: Calendar) -> Int { calendar.component(.weekday, from: noon(calendar)) }

    func days(to other: Day, calendar: Calendar) -> Int {
        calendar.dateComponents([.day], from: noon(calendar), to: other.noon(calendar)).day ?? 0
    }
}

struct TimeOfDay: Hashable, Comparable, CustomStringConvertible {
    let hour: Int
    let minute: Int

    init(_ hour: Int, _ minute: Int) {
        self.hour = min(max(hour, 0), 23)
        self.minute = min(max(minute, 0), 59)
    }

    init(_ date: Date, calendar: Calendar) {
        let parts = calendar.dateComponents([.hour, .minute], from: date)
        self.init(parts.hour ?? 0, parts.minute ?? 0)
    }

    static func valid(_ hour: Int, _ minute: Int) -> TimeOfDay? {
        guard (0...23).contains(hour), (0...59).contains(minute) else { return nil }
        return TimeOfDay(hour, minute)
    }

    var minutesSinceMidnight: Int { hour * 60 + minute }

    static func < (lhs: TimeOfDay, rhs: TimeOfDay) -> Bool { lhs.minutesSinceMidnight < rhs.minutesSinceMidnight }

    var description: String { "\(pad2(hour)):\(pad2(minute))" }
}

/// A reminder's due date: a day, optionally with a time. `nil` means "no date".
struct DueValue: Hashable, CustomStringConvertible {
    var day: Day
    var time: TimeOfDay?

    init(_ day: Day, _ time: TimeOfDay? = nil) {
        self.day = day
        self.time = time
    }

    init(instant: Date, calendar: Calendar) {
        self.init(Day(instant, calendar: calendar), TimeOfDay(instant, calendar: calendar))
    }

    var description: String { time.map { "\(day) \($0)" } ?? "\(day)" }

    /// When the reminder is due; an all-day reminder counts from the start of its day.
    func instant(_ calendar: Calendar) -> Date {
        guard let time else { return day.startOfDay(calendar) }
        return calendar.date(from: DateComponents(year: day.year, month: day.month, day: day.day,
                                                  hour: time.hour, minute: time.minute))
            ?? day.startOfDay(calendar)
    }
}

extension DueValue {
    /// Reads EventKit-style due components. Floating components (no time zone,
    /// which is how Reminders stores its own items) are taken at face value;
    /// components pinned to a zone are converted to the local zone.
    init?(components: DateComponents?, calendar: Calendar) {
        guard let components, let year = components.year, let month = components.month,
              let dayNumber = components.day,
              let day = Day.valid(year, month, dayNumber, calendar: calendar) else { return nil }
        guard let hour = components.hour else {
            self.init(day)
            return
        }
        let minute = components.minute ?? 0
        if let zone = components.timeZone {
            var source = Calendar(identifier: .gregorian)
            source.timeZone = zone
            guard TimeOfDay.valid(hour, minute) != nil,
                  let instant = source.date(from: DateComponents(year: year, month: month, day: dayNumber,
                                                                 hour: hour, minute: minute)) else { return nil }
            self.init(instant: instant, calendar: calendar)
        } else {
            guard let time = TimeOfDay.valid(hour, minute) else { return nil }
            self.init(day, time)
        }
    }

    /// Builds due components the way Reminders writes them: date-only for an
    /// all-day reminder, otherwise floating — unless the previous value was
    /// pinned to a zone, in which case it is re-pinned to the local zone so the
    /// local fields keep meaning what the user picked.
    func components(calendar: Calendar, previous: DateComponents?) -> DateComponents {
        var parts = DateComponents()
        parts.calendar = calendar
        parts.year = day.year
        parts.month = day.month
        parts.day = day.day
        if let time {
            parts.hour = time.hour
            parts.minute = time.minute
            parts.second = 0
            if previous?.timeZone != nil { parts.timeZone = calendar.timeZone }
        }
        return parts
    }
}

// MARK: - Edits

enum DayChange: Hashable {
    case keep
    case set(Day)
    /// Moves each reminder from its own day (today for an undated one).
    case shift(Int)
    case clear
}

enum TimeChange: Hashable {
    case keep
    case set(TimeOfDay)
    /// Makes the reminder all-day.
    case clear
}

struct DueEdit: Hashable, CustomStringConvertible {
    var day: DayChange
    var time: TimeChange

    init(day: DayChange = .keep, time: TimeChange = .keep) {
        self.day = day
        self.time = time
    }

    static let clear = DueEdit(day: .clear)

    var description: String { "DueEdit(day: \(day), time: \(time))" }
}

/// Applies an edit to one reminder's current due value.
///
/// Changing the day keeps each reminder's own time and changing the time keeps
/// its own day, so a multi-selection with mixed times moves together without
/// losing anything. Giving an undated reminder only a time puts it on `today`.
func applyEdit(_ edit: DueEdit, to current: DueValue?, today: Day, calendar: Calendar) -> DueValue? {
    let day: Day
    switch edit.day {
    case .clear:
        return nil
    case .set(let target):
        day = target
    case .shift(let count):
        day = (current?.day ?? today).adding(days: count, calendar: calendar)
    case .keep:
        if let current {
            day = current.day
        } else if case .set = edit.time {
            day = today
        } else {
            return nil
        }
    }
    switch edit.time {
    case .keep: return DueValue(day, current?.time)
    case .set(let time): return DueValue(day, time)
    case .clear: return DueValue(day, nil)
    }
}

/// Why an edit is not applied to a reminder.
enum EditBlock: Hashable {
    case readOnlyList
    case recurringNeedsDate
    case unreadableDate

    var message: String {
        switch self {
        case .readOnlyList: return "읽기 전용 목록이라 바꿀 수 없어요"
        case .recurringNeedsDate: return "반복 미리알림은 날짜를 없앨 수 없어요"
        case .unreadableDate: return "기존 날짜를 읽을 수 없어요. 날짜를 직접 골라 주세요"
        }
    }
}

/// EventKit refuses to save a recurring reminder without a due date, and a due
/// value this app cannot read must not be "kept" or shifted as if it were empty.
func blockReason(for edit: DueEdit, isRecurring: Bool, isEditable: Bool, hasUnreadableDue: Bool) -> EditBlock? {
    if !isEditable { return .readOnlyList }
    if case .clear = edit.day, isRecurring { return .recurringNeedsDate }
    if hasUnreadableDue {
        switch edit.day {
        case .set, .clear: return nil
        case .keep, .shift: return .unreadableDate
        }
    }
    return nil
}

// MARK: - Weeks and presets

/// Korean "이번 주 / 다음 주" count weeks from Monday, whatever the calendar
/// grid's first weekday is.
func mondayOfWeek(containing day: Day, calendar: Calendar) -> Day {
    day.adding(days: -((day.weekday(calendar) + 5) % 7), calendar: calendar)
}

/// This week's Saturday, or today when today is already Sunday.
func weekendDay(from today: Day, calendar: Calendar) -> Day {
    let saturday = mondayOfWeek(containing: today, calendar: calendar).adding(days: 5, calendar: calendar)
    return saturday < today ? today : saturday
}

func nextWeekMonday(from today: Day, calendar: Calendar) -> Day {
    mondayOfWeek(containing: today, calendar: calendar).adding(days: 7, calendar: calendar)
}

enum DuePreset: String, CaseIterable, Identifiable {
    case today, tomorrow, dayAfterTomorrow, thisWeekend, nextMonday
    case postponeDay, postponeWeek, advanceDay, clear

    var id: String { rawValue }

    var title: String {
        switch self {
        case .today: return "오늘"
        case .tomorrow: return "내일"
        case .dayAfterTomorrow: return "모레"
        case .thisWeekend: return "이번 주말"
        case .nextMonday: return "다음 주 월요일"
        case .postponeDay: return "하루 미루기"
        case .postponeWeek: return "일주일 미루기"
        case .advanceDay: return "하루 당기기"
        case .clear: return "날짜 없음"
        }
    }

    /// The day a day-setting preset lands on; `nil` for relative moves and "no date".
    func targetDay(today: Day, calendar: Calendar) -> Day? {
        switch self {
        case .today: return today
        case .tomorrow: return today.adding(days: 1, calendar: calendar)
        case .dayAfterTomorrow: return today.adding(days: 2, calendar: calendar)
        case .thisWeekend: return weekendDay(from: today, calendar: calendar)
        case .nextMonday: return nextWeekMonday(from: today, calendar: calendar)
        case .postponeDay, .postponeWeek, .advanceDay, .clear: return nil
        }
    }

    func edit(today: Day, calendar: Calendar) -> DueEdit {
        switch self {
        case .postponeDay: return DueEdit(day: .shift(1))
        case .postponeWeek: return DueEdit(day: .shift(7))
        case .advanceDay: return DueEdit(day: .shift(-1))
        case .clear: return .clear
        case .today, .tomorrow, .dayAfterTomorrow, .thisWeekend, .nextMonday:
            return DueEdit(day: .set(targetDay(today: today, calendar: calendar) ?? today))
        }
    }
}

// MARK: - Labels

let koreanWeekdaySymbols = ["일", "월", "화", "수", "목", "금", "토"]

private func weekdaySymbol(_ day: Day, calendar: Calendar) -> String {
    let weekday = day.weekday(calendar)
    return (1...7).contains(weekday) ? koreanWeekdaySymbols[weekday - 1] : ""
}

/// "10월 9일 (금)", with the year when it is not the current one.
func explicitDayLabel(_ day: Day, today: Day, calendar: Calendar) -> String {
    let weekday = weekdaySymbol(day, calendar: calendar)
    if day.year == today.year { return "\(day.month)월 \(day.day)일 (\(weekday))" }
    return "\(day.year)년 \(day.month)월 \(day.day)일 (\(weekday))"
}

func relativeDayWord(_ day: Day, today: Day, calendar: Calendar) -> String? {
    switch today.days(to: day, calendar: calendar) {
    case -1: return "어제"
    case 0: return "오늘"
    case 1: return "내일"
    case 2: return "모레"
    default: return nil
    }
}

func dayLabel(_ day: Day, today: Day, calendar: Calendar) -> String {
    relativeDayWord(day, today: today, calendar: calendar) ?? explicitDayLabel(day, today: today, calendar: calendar)
}

/// "오후 3:00" — the 12-hour form Korean Reminders uses (midnight is 오전 12:00).
func timeLabel(_ time: TimeOfDay) -> String {
    let period = time.hour < 12 ? "오전" : "오후"
    let hour = time.hour % 12 == 0 ? 12 : time.hour % 12
    return "\(period) \(hour):\(pad2(time.minute))"
}

/// Compact label for a list row: "내일 오후 3:00", "10월 9일 (금)", "날짜 없음".
func dueLabel(_ value: DueValue?, today: Day, calendar: Calendar) -> String {
    guard let value else { return "날짜 없음" }
    let day = dayLabel(value.day, today: today, calendar: calendar)
    guard let time = value.time else { return day }
    return "\(day) \(timeLabel(time))"
}

/// "내일 · 10월 1일 (목) 오후 3:00": the relative word when there is one, and always the exact date.
func fullDueLabel(_ value: DueValue?, today: Day, calendar: Calendar) -> String {
    guard let value else { return "날짜 없음" }
    var label = fullDayLabel(value.day, today: today, calendar: calendar)
    if let time = value.time { label += " \(timeLabel(time))" }
    return label
}

func fullDayLabel(_ day: Day, today: Day, calendar: Calendar) -> String {
    let explicit = explicitDayLabel(day, today: today, calendar: calendar)
    return relativeDayWord(day, today: today, calendar: calendar).map { "\($0) · \(explicit)" } ?? explicit
}

func monthTitle(_ day: Day) -> String { "\(day.year)년 \(day.month)월" }

func shiftLabel(_ count: Int) -> String {
    switch count {
    case 0: return "그대로"
    case 1: return "하루 미루기"
    case -1: return "하루 당기기"
    case 7: return "일주일 미루기"
    case -7: return "일주일 당기기"
    default: return count > 0 ? "\(count)일 미루기" : "\(-count)일 당기기"
    }
}

/// Describes an edit without a particular reminder, for a multi-selection preview.
func describeEdit(_ edit: DueEdit, today: Day, calendar: Calendar) -> String {
    var parts: [String] = []
    switch edit.day {
    case .clear: return "날짜 없음"
    case .set(let day): parts.append(fullDayLabel(day, today: today, calendar: calendar))
    case .shift(let count): parts.append(shiftLabel(count))
    case .keep: break
    }
    switch edit.time {
    case .set(let time): parts.append(timeLabel(time))
    case .clear: parts.append("종일")
    case .keep: break
    }
    return parts.joined(separator: " ")
}

// MARK: - Month grid, buckets and ordering

struct WeekdaySymbol: Hashable {
    let symbol: String
    /// 1 = Sunday … 7 = Saturday.
    let weekday: Int
}

func weekdayHeader(firstWeekday: Int) -> [WeekdaySymbol] {
    let first = (1...7).contains(firstWeekday) ? firstWeekday : 1
    return (0..<7).map { offset in
        let weekday = (first - 1 + offset) % 7 + 1
        return WeekdaySymbol(symbol: koreanWeekdaySymbols[weekday - 1], weekday: weekday)
    }
}

/// Always six full weeks, so the calendar keeps its height from month to month.
func monthGrid(year: Int, month: Int, firstWeekday: Int, calendar: Calendar) -> [Day] {
    let first = Day(year, month, 1)
    let firstColumn = (1...7).contains(firstWeekday) ? firstWeekday : 1
    let lead = (first.weekday(calendar) - firstColumn + 7) % 7
    let start = first.adding(days: -lead, calendar: calendar)
    return (0..<42).map { start.adding(days: $0, calendar: calendar) }
}

enum DueBucket: Int, CaseIterable, Comparable, Identifiable {
    case overdue, today, tomorrow, thisWeek, later, undated

    var id: Int { rawValue }

    var title: String {
        switch self {
        case .overdue: return "지연됨"
        case .today: return "오늘"
        case .tomorrow: return "내일"
        case .thisWeek: return "7일 이내"
        case .later: return "나중에"
        case .undated: return "날짜 없음"
        }
    }

    static func < (lhs: DueBucket, rhs: DueBucket) -> Bool { lhs.rawValue < rhs.rawValue }
}

/// A timed reminder whose time has passed today is overdue, as Reminders shows it in red.
func bucket(for value: DueValue?, now: Date, calendar: Calendar) -> DueBucket {
    guard let value else { return .undated }
    let today = Day(now, calendar: calendar)
    if value.day < today { return .overdue }
    if value.day == today {
        return value.time != nil && value.instant(calendar) < now ? .overdue : .today
    }
    let distance = today.days(to: value.day, calendar: calendar)
    if distance == 1 { return .tomorrow }
    return distance <= 7 ? .thisWeek : .later
}

/// Earlier first, all-day before timed on the same day, undated last.
/// Returns `nil` when both sort the same, so the caller can break the tie.
func dueSortsBefore(_ lhs: DueValue?, _ rhs: DueValue?) -> Bool? {
    switch (lhs, rhs) {
    case (nil, nil):
        return nil
    case (nil, _):
        return false
    case (_, nil):
        return true
    case let (left?, right?):
        if left.day != right.day { return left.day < right.day }
        let leftMinutes = left.time?.minutesSinceMidnight ?? -1
        let rightMinutes = right.time?.minutesSinceMidnight ?? -1
        return leftMinutes == rightMinutes ? nil : leftMinutes < rightMinutes
    }
}

// MARK: - Alarms and start date that follow the due date

/// Changes to a reminder's absolute (clock-time) alarms when its due date moves.
struct AlarmPlan: Hashable {
    /// Indices into the absolute-alarm list that was planned against.
    var remove: [Int] = []
    var add: [Date] = []

    var isEmpty: Bool { remove.isEmpty && add.isEmpty }
}

let alarmMatchTolerance: TimeInterval = 60

/// Reminders implements "notify at the due time" as an absolute alarm equal to
/// the due instant, so moving the due date alone would leave the notification
/// behind. An alarm is anchored to the due date when it sits on the old due
/// instant (timed) or on the old due day (all-day). Anchored alarms follow the
/// date; every other alarm (relative offsets, locations, unrelated clock times)
/// is left as it is.
///
/// - timed → timed: an anchored alarm moves to the new instant. With no
///   anchored alarm the user had no notification, and none is added.
/// - timed → all-day: the anchored alarm is removed (all-day reminders notify
///   through Reminders' own "all-day notification" setting).
/// - all-day or undated → timed: an alarm is added at the new time, like setting
///   a time in Reminders does; anchored day alarms are replaced by it.
/// - all-day → all-day: anchored alarms move by the same number of days.
/// - any → no date: anchored alarms are removed.
func planAlarms(oldDue: DueValue?, newDue: DueValue?, absoluteAlarms: [Date], calendar: Calendar) -> AlarmPlan {
    guard oldDue != newDue else { return AlarmPlan() }
    var anchored: [Int] = []
    if let old = oldDue {
        if old.time != nil {
            let oldInstant = old.instant(calendar)
            anchored = absoluteAlarms.indices.filter {
                abs(absoluteAlarms[$0].timeIntervalSince(oldInstant)) < alarmMatchTolerance
            }
        } else {
            anchored = absoluteAlarms.indices.filter { Day(absoluteAlarms[$0], calendar: calendar) == old.day }
        }
    }

    var plan = AlarmPlan()
    switch (oldDue, newDue) {
    case (_, nil):
        plan.remove = anchored
    case (nil, let new?):
        if new.time != nil { plan.add = [new.instant(calendar)] }
    case let (old?, new?):
        switch (old.time != nil, new.time != nil) {
        case (true, true):
            if !anchored.isEmpty {
                plan.remove = anchored
                plan.add = [new.instant(calendar)]
            }
        case (true, false):
            plan.remove = anchored
        case (false, true):
            plan.remove = anchored
            plan.add = [new.instant(calendar)]
        case (false, false):
            let delta = old.day.days(to: new.day, calendar: calendar)
            plan.remove = anchored
            plan.add = anchored.compactMap { calendar.date(byAdding: .day, value: delta, to: absoluteAlarms[$0]) }
        }
    }

    // Never add an alarm that duplicates one that stays or another added one.
    let kept = absoluteAlarms.indices.filter { !plan.remove.contains($0) }.map { absoluteAlarms[$0] }
    var additions: [Date] = []
    for date in plan.add where !(kept + additions).contains(where: { abs($0.timeIntervalSince(date)) < alarmMatchTolerance }) {
        additions.append(date)
    }
    plan.add = additions
    return plan
}

enum StartPlan: Hashable {
    case keep
    case clear
    case set(DueValue)
}

/// A reminder's start date is kept unless it mirrored the old due date (then it
/// follows it), would end up after the new due date (then it is pulled to it),
/// or the due date is removed (then it goes too).
func planStart(start: DueValue?, oldDue: DueValue?, newDue: DueValue?, calendar: Calendar) -> StartPlan {
    guard let start else { return .keep }
    guard let new = newDue else { return .clear }
    if let old = oldDue, start.day == old.day, old.time == nil || start.time == old.time {
        return start == new ? .keep : .set(new)
    }
    let inverted = new.time != nil
        ? start.instant(calendar) > new.instant(calendar)
        : start.day > new.day
    return inverted ? .set(new) : .keep
}

/// Start components for a moved start date that keep the shape of the old
/// start: Reminders stores an all-day reminder's start as 00:00 of its day, so
/// a start that carried a clock time keeps carrying one, and a date-only start
/// stays date-only. A start pinned to a zone is re-pinned to the local zone.
func startComponents(for value: DueValue, previous: DateComponents?, calendar: Calendar) -> DateComponents {
    var parts = DateComponents()
    parts.calendar = calendar
    parts.year = value.day.year
    parts.month = value.day.month
    parts.day = value.day.day
    let previousValue = DueValue(components: previous, calendar: calendar)
    if let time = value.time ?? previousValue?.time {
        parts.hour = time.hour
        parts.minute = time.minute
        parts.second = 0
        if previous?.timeZone != nil { parts.timeZone = calendar.timeZone }
    }
    return parts
}

// MARK: - Korean natural-language input

enum ParseOutcome: Hashable {
    case empty
    case edit(DueEdit)
    case failure(String)
}

private struct TextCursor {
    private(set) var rest: String

    init(_ text: String) { rest = text }

    var isAtEnd: Bool { rest.isEmpty }

    /// Consumes `pattern` when it matches at the start of the remaining text and
    /// returns its capture groups ("" for a group that did not take part).
    mutating func take(_ pattern: String) -> [String]? {
        guard !rest.isEmpty,
              let regex = try? NSRegularExpression(pattern: "^(?:" + pattern + ")", options: [.caseInsensitive]) else {
            return nil
        }
        let text = NSString(string: rest)
        guard let match = regex.firstMatch(in: rest, options: [], range: NSRange(location: 0, length: text.length)),
              match.range.location == 0, match.range.length > 0 else { return nil }
        var groups: [String] = []
        if match.numberOfRanges > 1 {
            for index in 1..<match.numberOfRanges {
                let range = match.range(at: index)
                groups.append(range.location == NSNotFound ? "" : text.substring(with: range))
            }
        }
        rest = String(text.substring(from: match.range.length).drop(while: { $0 == " " }))
        return groups
    }

    mutating func skipParticles() {
        while take("까지|으로|에는|즈음|에|로|쯤|경") != nil {}
    }
}

private func replacingMatches(_ pattern: String, in text: String, transform: ([String]) -> String) -> String {
    guard let regex = try? NSRegularExpression(pattern: pattern, options: [.caseInsensitive]) else { return text }
    let source = NSString(string: text)
    var result = ""
    var position = 0
    for match in regex.matches(in: text, options: [], range: NSRange(location: 0, length: source.length)) {
        result += source.substring(with: NSRange(location: position, length: match.range.location - position))
        var groups: [String] = []
        if match.numberOfRanges > 1 {
            for index in 1..<match.numberOfRanges {
                let range = match.range(at: index)
                groups.append(range.location == NSNotFound ? "" : source.substring(with: range))
            }
        }
        result += transform(groups)
        position = match.range.location + match.range.length
    }
    result += source.substring(from: position)
    return result
}

private let nativeNumbers: [String: Int] = [
    "한": 1, "두": 2, "세": 3, "네": 4, "다섯": 5, "여섯": 6,
    "일곱": 7, "여덟": 8, "아홉": 9, "열": 10, "열한": 11, "열두": 12,
]

private let clearCommands: Set<String> = [
    "없음", "날짜없음", "날짜삭제", "날짜지우기", "날짜해제", "날짜없애기",
    "삭제", "지우기", "해제", "없애기", "clear", "none",
]

/// Lower-cases, collapses spaces and rewrites spoken numbers ("세시 반",
/// "이틀 뒤", "일주일 후") into the digit forms the grammar reads.
private func normalizedDueText(_ raw: String) -> String {
    var text = raw.lowercased()
        .split(whereSeparator: { $0.isWhitespace })
        .joined(separator: " ")
    while let last = text.last, "!?,.~".contains(last) { text.removeLast() }
    text = text.trimmingCharacters(in: .whitespaces)
    let words: [(String, String)] = [
        ("\\btoday\\b", "오늘"), ("\\btomorrow\\b|\\btmr\\b", "내일"),
        ("하루\\s?종일", "종일"), ("일주일", "1주"), ("하루", "1일"), ("이틀", "2일"),
        ("사흘", "3일"), ("나흘", "4일"), ("닷새", "5일"), ("열흘", "10일"),
    ]
    for (pattern, replacement) in words {
        text = replacingMatches(pattern, in: text) { _ in replacement }
    }
    text = replacingMatches("(열두|열한|열|아홉|여덟|일곱|여섯|다섯|네|세|두|한)\\s?시", in: text) { groups in
        "\(nativeNumbers[groups[0]] ?? 0)시"
    }
    text = replacingMatches("(한|두|세)\\s?(주|달)", in: text) { groups in
        "\(nativeNumbers[groups[0]] ?? 0)\(groups[1])"
    }
    return text
}

private enum DayPart {
    case success(DayChange)
    case failure(String)
}

private enum TimePart {
    case time(TimeChange, carryDays: Int)
    case instant(Date)
    case failure(String)
}

private let weekWords = "이번\\s?주|금주|다음\\s?주|담주|다다음\\s?주|지난\\s?주|저번\\s?주"

/// Monday-based week offset for "이번 주 / 다음 주 / 다다음 주 / 지난 주".
private func weekOffset(_ word: String) -> Int {
    let compact = word.replacingOccurrences(of: " ", with: "")
    switch compact {
    case "다음주", "담주": return 1
    case "다다음주": return 2
    case "지난주", "저번주": return -1
    default: return 0
    }
}

private func weekdayIndexFromMonday(_ symbol: String) -> Int? {
    ["월", "화", "수", "목", "금", "토", "일"].firstIndex(of: symbol)
}

/// The first M월 D일 on or after today, trying later years for 2월 29일.
private func upcomingMonthDay(month: Int, day: Int, today: Day, calendar: Calendar) -> DayPart {
    for offset in 0...8 {
        if let candidate = Day.valid(today.year + offset, month, day, calendar: calendar), candidate >= today {
            return .success(.set(candidate))
        }
    }
    return .failure("\(month)월 \(day)일은 없는 날짜예요")
}

private func parseDayPart(_ cursor: inout TextCursor, today: Day, calendar: Calendar) -> DayPart? {
    func relative(_ days: Int) -> DayPart { .success(.set(today.adding(days: days, calendar: calendar))) }

    if cursor.take("오늘|금일") != nil { return relative(0) }
    if cursor.take("내일\\s?모레") != nil { return relative(2) }
    if cursor.take("내일|명일") != nil { return relative(1) }
    if cursor.take("모레") != nil { return relative(2) }
    if cursor.take("글피") != nil { return relative(3) }
    if cursor.take("어제") != nil { return relative(-1) }
    if cursor.take("그저께|그제") != nil { return relative(-2) }

    // "+2", "-1", "+1주": move each reminder from its own date.
    if let groups = cursor.take("([+-])\\s?(\\d{1,3})\\s?(일|주)?") {
        let count = (Int(groups[1]) ?? 0) * (groups[2] == "주" ? 7 : 1)
        return .success(.shift(groups[0] == "-" ? -count : count))
    }

    // "3일 후", "2주 뒤", "1달 후", "5일 안에"
    if let groups = cursor.take("(\\d{1,3})\\s?(일|주일|주|개월|달)\\s?(?:후|뒤|있다가|이내|안에|내)") {
        let count = Int(groups[0]) ?? 0
        switch groups[1] {
        case "일": return relative(count)
        case "주", "주일": return relative(count * 7)
        default: return .success(.set(today.adding(months: count, calendar: calendar)))
        }
    }

    // "2026-10-15", "2026.10.15", "2026년 10월 15일"
    if let groups = cursor.take("(\\d{4})\\s?(?:[-./]|년)\\s?(\\d{1,2})\\s?(?:[-./]|월)\\s?(\\d{1,2})\\s?일?") {
        let year = Int(groups[0]) ?? 0, month = Int(groups[1]) ?? 0, day = Int(groups[2]) ?? 0
        guard let target = Day.valid(year, month, day, calendar: calendar) else {
            return .failure("\(year)년 \(month)월 \(day)일은 없는 날짜예요")
        }
        return .success(.set(target))
    }

    // "내년 3월 1일", "올해 12월 25일"
    if let groups = cursor.take("(내년|올해|금년)\\s?(\\d{1,2})\\s?(?:월|[-./])\\s?(\\d{1,2})\\s?일?") {
        let year = today.year + (groups[0] == "내년" ? 1 : 0)
        let month = Int(groups[1]) ?? 0, day = Int(groups[2]) ?? 0
        guard let target = Day.valid(year, month, day, calendar: calendar) else {
            return .failure("\(year)년 \(month)월 \(day)일은 없는 날짜예요")
        }
        return .success(.set(target))
    }

    // "10월 15일", "10/15", "10.15", "10-15": the next time that date comes around.
    if let groups = cursor.take("(\\d{1,2})\\s?월\\s?(\\d{1,2})\\s?일?")
        ?? cursor.take("(\\d{1,2})[/.-](\\d{1,2})(?![\\d:])") {
        return upcomingMonthDay(month: Int(groups[0]) ?? 0, day: Int(groups[1]) ?? 0, today: today, calendar: calendar)
    }

    // "이번 달 말", "다음 달 15일", "다음 달"
    if let groups = cursor.take("(이번\\s?달|이달|금월|다음\\s?달|담달|다다음\\s?달|지난\\s?달|저번\\s?달)(?:\\s?(말|마지막\\s?날)|\\s?(\\d{1,2})\\s?일)?") {
        let word = groups[0].replacingOccurrences(of: " ", with: "")
        let offset: Int
        switch word {
        case "다음달", "담달": offset = 1
        case "다다음달": offset = 2
        case "지난달", "저번달": offset = -1
        default: offset = 0
        }
        let anchor = Day(today.year, today.month, 1).adding(months: offset, calendar: calendar)
        if !groups[1].isEmpty { return .success(.set(anchor.lastDayOfMonth(calendar))) }
        if let dayNumber = Int(groups[2]) {
            guard let target = Day.valid(anchor.year, anchor.month, dayNumber, calendar: calendar) else {
                return .failure("\(anchor.month)월 \(dayNumber)일은 없는 날짜예요")
            }
            return .success(.set(target))
        }
        guard offset != 0 else { return .failure("이번 달 며칠인지 함께 적어 주세요 (예: 이번 달 20일)") }
        return .success(.set(today.adding(months: offset, calendar: calendar)))
    }
    if cursor.take("월말|말일") != nil { return .success(.set(today.lastDayOfMonth(calendar))) }

    // "15일": the next 15th, this month or later.
    if let groups = cursor.take("(\\d{1,2})\\s?일(?!\\s?간)") {
        let dayNumber = Int(groups[0]) ?? 0
        for offset in 0...12 {
            let month = Day(today.year, today.month, 1).adding(months: offset, calendar: calendar)
            if let candidate = Day.valid(month.year, month.month, dayNumber, calendar: calendar), candidate >= today {
                return .success(.set(candidate))
            }
        }
        return .failure("\(dayNumber)일은 없는 날짜예요")
    }

    // "다음 주 주말", then "이번 주말", "다음 주말", "주말"
    let monday = mondayOfWeek(containing: today, calendar: calendar)
    if let groups = cursor.take("(\(weekWords))\\s?주말") {
        let offset = weekOffset(groups[0])
        if offset == 0 { return .success(.set(weekendDay(from: today, calendar: calendar))) }
        return .success(.set(monday.adding(days: offset * 7 + 5, calendar: calendar)))
    }
    if let groups = cursor.take("(이번|다음|담|다다음|지난|저번)?\\s?주말") {
        let offset: Int
        switch groups[0] {
        case "다음", "담": offset = 1
        case "다다음": offset = 2
        case "지난", "저번": offset = -1
        default: offset = 0
        }
        if offset == 0 { return .success(.set(weekendDay(from: today, calendar: calendar))) }
        return .success(.set(monday.adding(days: offset * 7 + 5, calendar: calendar)))
    }

    // "다음 주 금요일", "이번 주 월", "금요일" (next one after today), "다음 월요일"
    // "(?!주)" keeps "금주" (this week) from being read as 금요일.
    if let groups = cursor.take("(\(weekWords)|다음|이번|오는)?\\s?(월|화|수|목|금|토|일)(?:요일|욜)?(?!주)") {
        guard let index = weekdayIndexFromMonday(groups[1]) else { return nil }
        let prefix = groups[0].replacingOccurrences(of: " ", with: "")
        if prefix.hasSuffix("주") {
            return .success(.set(monday.adding(days: weekOffset(prefix) * 7 + index, calendar: calendar)))
        }
        let target = (index + 1) % 7 + 1 // Monday-based index to Calendar weekday
        var delta = (target - today.weekday(calendar) + 7) % 7
        if delta == 0 { delta = 7 }
        return .success(.set(today.adding(days: delta, calendar: calendar)))
    }

    // "다음 주" alone: that week's Monday.
    if let groups = cursor.take("(\(weekWords))") {
        let offset = weekOffset(groups[0])
        guard offset != 0 else { return .failure("이번 주 무슨 요일인지 함께 적어 주세요 (예: 이번 주 금요일)") }
        return .success(.set(monday.adding(days: offset * 7, calendar: calendar)))
    }
    return nil
}

private let periodWords = "오전|오후|아침|점심|낮|저녁|밤|새벽"

/// Converts a spoken hour with its part of day into a 24-hour time. A bare
/// "3시" or "3:30" means the afternoon (1–6시 are read that way, as people say
/// them); a zero-padded "03:30" is taken literally. "밤 1시" and "자정" belong
/// to the night after the given day.
private func resolveHour(period: String, hour: Int, minute: Int, literal: Bool) -> (TimeOfDay, Int)? {
    guard (0...59).contains(minute) else { return nil }
    func at(_ h: Int, carry: Int = 0) -> (TimeOfDay, Int)? {
        TimeOfDay.valid(h, minute).map { ($0, carry) }
    }
    switch period {
    case "":
        if literal { return at(hour) }
        switch hour {
        case 0: return at(0)
        case 1...6: return at(hour + 12)
        case 7...23: return at(hour)
        default: return nil
        }
    case "오전", "아침":
        switch hour {
        case 12: return at(0)
        case 0...11: return at(hour)
        default: return nil
        }
    case "오후":
        switch hour {
        case 1...11: return at(hour + 12)
        case 12...23: return at(hour)
        default: return nil
        }
    case "점심", "낮":
        switch hour {
        case 11, 12: return at(hour)
        case 1...10: return at(hour + 12)
        case 13...23: return at(hour)
        default: return nil
        }
    case "저녁":
        switch hour {
        case 1...11: return at(hour + 12)
        case 12: return at(0, carry: 1)
        case 13...23: return at(hour)
        default: return nil
        }
    case "밤":
        switch hour {
        case 0...5: return at(hour, carry: 1)
        case 6...11: return at(hour + 12)
        case 12: return at(0, carry: 1)
        case 13...23: return at(hour)
        default: return nil
        }
    case "새벽":
        switch hour {
        case 12: return at(0)
        case 0...7: return at(hour)
        default: return nil
        }
    default:
        return nil
    }
}

private func defaultTime(for period: String) -> TimeOfDay {
    switch period {
    case "아침": return TimeOfDay(8, 0)
    case "오전": return TimeOfDay(9, 0)
    case "점심", "낮": return TimeOfDay(12, 0)
    case "오후": return TimeOfDay(15, 0)
    case "저녁": return TimeOfDay(19, 0)
    case "밤": return TimeOfDay(21, 0)
    default: return TimeOfDay(6, 0) // 새벽
    }
}

private func parseTimePart(_ cursor: inout TextCursor, now: Date) -> TimePart? {
    let later = "\\s?(?:후|뒤|있다가|이내|안에|내)"
    if let groups = cursor.take("(\\d{1,3})\\s?시간(?:\\s?(\\d{1,2})\\s?분|\\s?(반))?" + later) {
        let minutes = (Int(groups[0]) ?? 0) * 60 + (Int(groups[1]) ?? 0) + (groups[2].isEmpty ? 0 : 30)
        return .instant(now.addingTimeInterval(TimeInterval(minutes * 60)))
    }
    if let groups = cursor.take("(\\d{1,4})\\s?분" + later) {
        return .instant(now.addingTimeInterval(TimeInterval((Int(groups[0]) ?? 0) * 60)))
    }
    if cursor.take("종일|시간\\s?없이|시간\\s?없음|시간\\s?삭제|시간\\s?해제") != nil {
        return .time(.clear, carryDays: 0)
    }
    if cursor.take("정오") != nil { return .time(.set(TimeOfDay(12, 0)), carryDays: 0) }
    if cursor.take("자정") != nil { return .time(.set(TimeOfDay(0, 0)), carryDays: 1) }

    if let groups = cursor.take("(\(periodWords))?\\s?(\\d{1,2}):(\\d{2})") {
        guard let resolved = resolveHour(period: groups[0], hour: Int(groups[1]) ?? -1,
                                         minute: Int(groups[2]) ?? -1, literal: groups[1].count == 2) else {
            return .failure("\(groups[1]):\(groups[2])은(는) 없는 시각이에요")
        }
        return .time(.set(resolved.0), carryDays: resolved.1)
    }
    if let groups = cursor.take("(\(periodWords))?\\s?(\\d{1,2})\\s?시(?!\\s?간)(?:\\s?(반)|\\s?(\\d{1,2})\\s?분)?") {
        let minute = groups[2].isEmpty ? (Int(groups[3]) ?? 0) : 30
        guard let resolved = resolveHour(period: groups[0], hour: Int(groups[1]) ?? -1,
                                         minute: minute, literal: false) else {
            return .failure("\(groups[0].isEmpty ? "" : groups[0] + " ")\(groups[1])시 \(minute)분은 없는 시각이에요")
        }
        return .time(.set(resolved.0), carryDays: resolved.1)
    }
    if let groups = cursor.take("(\(periodWords))") {
        return .time(.set(defaultTime(for: groups[0])), carryDays: 0)
    }
    return nil
}

/// Parses Korean date input such as "내일", "다음 주 금 오후 3시", "10/15",
/// "3일 후", "2시간 뒤", "종일" or "없음" into an edit.
///
/// Relative days ("3일 후", "금요일") count from today. A time alone keeps each
/// reminder's own day, a day alone keeps its own time.
func parseDueText(_ raw: String, now: Date, calendar: Calendar) -> ParseOutcome {
    let text = normalizedDueText(raw)
    guard !text.isEmpty else { return .empty }
    if clearCommands.contains(text.replacingOccurrences(of: " ", with: "")) { return .edit(.clear) }

    let today = Day(now, calendar: calendar)
    var cursor = TextCursor(text)
    var dayChange: DayChange?
    var timeChange: TimeChange?
    var carry = 0
    var instant: Date?

    for _ in 0..<3 where !cursor.isAtEnd {
        var progressed = false
        if dayChange == nil, instant == nil, let part = parseDayPart(&cursor, today: today, calendar: calendar) {
            switch part {
            case .success(let change): dayChange = change
            case .failure(let message): return .failure(message)
            }
            cursor.skipParticles()
            progressed = true
        }
        if timeChange == nil, instant == nil, let part = parseTimePart(&cursor, now: now) {
            switch part {
            case .time(let change, let days):
                timeChange = change
                carry = days
            case .instant(let moment):
                guard dayChange == nil else { return .failure("‘몇 시간 후’는 날짜와 함께 쓸 수 없어요") }
                instant = moment
            case .failure(let message):
                return .failure(message)
            }
            cursor.skipParticles()
            progressed = true
        }
        if !progressed { break }
    }

    guard cursor.isAtEnd else { return .failure("‘\(cursor.rest)’ 부분을 이해하지 못했어요") }
    if let instant {
        let value = DueValue(instant: instant, calendar: calendar)
        return .edit(DueEdit(day: .set(value.day), time: value.time.map(TimeChange.set) ?? .keep))
    }
    guard dayChange != nil || timeChange != nil else { return .failure("날짜나 시간을 찾지 못했어요") }
    var day = dayChange ?? .keep
    if carry != 0 {
        switch day {
        case .keep: day = .shift(carry)
        case .set(let target): day = .set(target.adding(days: carry, calendar: calendar))
        case .shift(let count): day = .shift(count + carry)
        case .clear: break
        }
    }
    return .edit(DueEdit(day: day, time: timeChange ?? .keep))
}
