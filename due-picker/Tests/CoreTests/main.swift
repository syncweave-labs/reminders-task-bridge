// Test runner for DueCore.swift. Built by scripts/test-due-picker.sh with
// plain swiftc (no XCTest), so it runs the same on macOS and in Linux CI.

import Foundation

final class Results {
    var passed = 0
    var failed = 0
}

let results = Results()

func expect(_ condition: Bool, _ message: @autoclosure () -> String, line: Int = #line) {
    if condition {
        results.passed += 1
    } else {
        results.failed += 1
        print("FAIL line \(line): \(message())")
    }
}

func expectEqual<T: Equatable>(_ actual: T, _ expected: T, _ label: String = "", line: Int = #line) {
    expect(actual == expected, "\(label) expected \(expected), got \(actual)", line: line)
}

var calendar = Calendar(identifier: .gregorian)
calendar.timeZone = TimeZone(identifier: "Asia/Seoul")!
calendar.firstWeekday = 1

func at(_ year: Int, _ month: Int, _ day: Int, _ hour: Int = 0, _ minute: Int = 0, zone: String = "Asia/Seoul") -> Date {
    var zoned = Calendar(identifier: .gregorian)
    zoned.timeZone = TimeZone(identifier: zone)!
    return zoned.date(from: DateComponents(year: year, month: month, day: day, hour: hour, minute: minute))!
}

func d(_ year: Int, _ month: Int, _ day: Int) -> Day { Day(year, month, day) }
func t(_ hour: Int, _ minute: Int = 0) -> TimeOfDay { TimeOfDay(hour, minute) }

// Wednesday 2026-09-30 18:00 in Seoul.
let now = at(2026, 9, 30, 18, 0)
let today = Day(now, calendar: calendar)

// MARK: Day arithmetic

expectEqual(today, d(2026, 9, 30), "today")
expectEqual(today.weekday(calendar), 4, "2026-09-30 is a Wednesday")
expectEqual(d(2026, 9, 30).adding(days: 1, calendar: calendar), d(2026, 10, 1))
expectEqual(d(2026, 12, 31).adding(days: 1, calendar: calendar), d(2027, 1, 1))
expectEqual(d(2026, 3, 1).adding(days: -1, calendar: calendar), d(2026, 2, 28))
expectEqual(d(2027, 1, 31).adding(months: 1, calendar: calendar), d(2027, 2, 28), "month clamp")
expectEqual(d(2028, 1, 31).adding(months: 1, calendar: calendar), d(2028, 2, 29), "leap month clamp")
expectEqual(d(2026, 12, 15).adding(months: 1, calendar: calendar), d(2027, 1, 15))
expectEqual(d(2026, 1, 15).adding(months: -1, calendar: calendar), d(2025, 12, 15))
expectEqual(d(2026, 9, 30).days(to: d(2026, 10, 1), calendar: calendar), 1)
expectEqual(d(2026, 10, 1).days(to: d(2026, 9, 28), calendar: calendar), -3)
expectEqual(d(2026, 12, 31).days(to: d(2027, 1, 1), calendar: calendar), 1)
expectEqual(Day.daysInMonth(year: 2026, month: 2, calendar: calendar), 28)
expectEqual(Day.daysInMonth(year: 2028, month: 2, calendar: calendar), 29)
expectEqual(Day.valid(2026, 2, 30, calendar: calendar), nil, "no 2월 30일")
expectEqual(Day.valid(2026, 13, 1, calendar: calendar), nil, "no 13월")
expectEqual(d(2026, 9, 12).lastDayOfMonth(calendar), d(2026, 9, 30))
expectEqual(mondayOfWeek(containing: today, calendar: calendar), d(2026, 9, 28))
expectEqual(mondayOfWeek(containing: d(2026, 10, 4), calendar: calendar), d(2026, 9, 28), "Sunday belongs to the Monday-first week")
expectEqual(weekendDay(from: today, calendar: calendar), d(2026, 10, 3))
expectEqual(weekendDay(from: d(2026, 10, 4), calendar: calendar), d(2026, 10, 4), "on Sunday the weekend is today")
expectEqual(nextWeekMonday(from: today, calendar: calendar), d(2026, 10, 5))
expectEqual(nextWeekMonday(from: d(2026, 10, 4), calendar: calendar), d(2026, 10, 5))

// MARK: Applying edits

let tomorrowAtThree = DueValue(d(2026, 10, 1), t(15))
expectEqual(applyEdit(DueEdit(day: .set(d(2026, 10, 5))), to: tomorrowAtThree, today: today, calendar: calendar),
            DueValue(d(2026, 10, 5), t(15)), "changing the day keeps the time")
expectEqual(applyEdit(DueEdit(day: .set(d(2026, 10, 5))), to: nil, today: today, calendar: calendar),
            DueValue(d(2026, 10, 5)), "undated becomes all-day")
expectEqual(applyEdit(DueEdit(time: .set(t(15))), to: nil, today: today, calendar: calendar),
            DueValue(today, t(15)), "a time alone puts an undated reminder on today")
expectEqual(applyEdit(DueEdit(time: .clear), to: nil, today: today, calendar: calendar), nil)
expectEqual(applyEdit(DueEdit(), to: nil, today: today, calendar: calendar), nil)
expectEqual(applyEdit(DueEdit(time: .set(t(9, 30))), to: tomorrowAtThree, today: today, calendar: calendar),
            DueValue(d(2026, 10, 1), t(9, 30)), "changing the time keeps the day")
expectEqual(applyEdit(DueEdit(time: .clear), to: tomorrowAtThree, today: today, calendar: calendar),
            DueValue(d(2026, 10, 1)), "종일 drops the time")
expectEqual(applyEdit(DueEdit(day: .shift(1)), to: tomorrowAtThree, today: today, calendar: calendar),
            DueValue(d(2026, 10, 2), t(15)), "shift moves from the reminder's own day")
expectEqual(applyEdit(DueEdit(day: .shift(1)), to: nil, today: today, calendar: calendar),
            DueValue(d(2026, 10, 1)), "shift on undated counts from today")
expectEqual(applyEdit(.clear, to: tomorrowAtThree, today: today, calendar: calendar), nil)

// MARK: Presets

expectEqual(DuePreset.today.edit(today: today, calendar: calendar), DueEdit(day: .set(today)))
expectEqual(DuePreset.tomorrow.edit(today: today, calendar: calendar), DueEdit(day: .set(d(2026, 10, 1))))
expectEqual(DuePreset.dayAfterTomorrow.edit(today: today, calendar: calendar), DueEdit(day: .set(d(2026, 10, 2))))
expectEqual(DuePreset.thisWeekend.edit(today: today, calendar: calendar), DueEdit(day: .set(d(2026, 10, 3))))
expectEqual(DuePreset.nextMonday.edit(today: today, calendar: calendar), DueEdit(day: .set(d(2026, 10, 5))))
expectEqual(DuePreset.postponeDay.edit(today: today, calendar: calendar), DueEdit(day: .shift(1)))
expectEqual(DuePreset.postponeWeek.edit(today: today, calendar: calendar), DueEdit(day: .shift(7)))
expectEqual(DuePreset.advanceDay.edit(today: today, calendar: calendar), DueEdit(day: .shift(-1)))
expectEqual(DuePreset.clear.edit(today: today, calendar: calendar), DueEdit.clear)

// MARK: Blocked edits

expectEqual(blockReason(for: .clear, isRecurring: true, isEditable: true, hasUnreadableDue: false), .recurringNeedsDate)
expectEqual(blockReason(for: DueEdit(day: .set(today)), isRecurring: true, isEditable: true, hasUnreadableDue: false), nil)
expectEqual(blockReason(for: DueEdit(day: .set(today)), isRecurring: false, isEditable: false, hasUnreadableDue: false), .readOnlyList)
expectEqual(blockReason(for: DueEdit(time: .set(t(9))), isRecurring: false, isEditable: true, hasUnreadableDue: true), .unreadableDate)
expectEqual(blockReason(for: DueEdit(day: .shift(1)), isRecurring: false, isEditable: true, hasUnreadableDue: true), .unreadableDate)
expectEqual(blockReason(for: DueEdit(day: .set(today)), isRecurring: false, isEditable: true, hasUnreadableDue: true), nil)
expectEqual(blockReason(for: .clear, isRecurring: false, isEditable: true, hasUnreadableDue: true), nil)

// MARK: Parsing

func parse(_ text: String) -> ParseOutcome { parseDueText(text, now: now, calendar: calendar) }

func expectParse(_ text: String, _ day: DayChange, _ time: TimeChange = .keep, _ note: String = "", line: Int = #line) {
    expectEqual(parse(text), .edit(DueEdit(day: day, time: time)), "parse(\(text)) \(note)", line: line)
}

func expectParse(_ text: String, _ day: DayChange, _ note: String, line: Int = #line) {
    expectParse(text, day, .keep, note, line: line)
}

func expectFailure(_ text: String, line: Int = #line) {
    if case .failure = parse(text) {
        results.passed += 1
    } else {
        results.failed += 1
        print("FAIL line \(line): parse(\(text)) should fail, got \(parse(text))")
    }
}

expectEqual(parse(""), .empty)
expectEqual(parse("   "), .empty)

// Relative day words
expectParse("오늘", .set(today))
expectParse("today", .set(today))
expectParse("내일", .set(d(2026, 10, 1)))
expectParse("tomorrow", .set(d(2026, 10, 1)))
expectParse("모레", .set(d(2026, 10, 2)))
expectParse("내일모레", .set(d(2026, 10, 2)))
expectParse("내일 모레", .set(d(2026, 10, 2)))
expectParse("글피", .set(d(2026, 10, 3)))
expectParse("어제", .set(d(2026, 9, 29)))
expectParse("내일까지", .set(d(2026, 10, 1)))
expectParse("내일.", .set(d(2026, 10, 1)))

// Counted days from today
expectParse("3일 후", .set(d(2026, 10, 3)))
expectParse("3일후", .set(d(2026, 10, 3)))
expectParse("3일 뒤", .set(d(2026, 10, 3)))
expectParse("5일 안에", .set(d(2026, 10, 5)))
expectParse("2주 후", .set(d(2026, 10, 14)))
expectParse("1주일 뒤", .set(d(2026, 10, 7)))
expectParse("일주일 뒤", .set(d(2026, 10, 7)))
expectParse("1달 후", .set(d(2026, 10, 30)))
expectParse("한 달 뒤", .set(d(2026, 10, 30)))
expectParse("1개월 뒤", .set(d(2026, 10, 30)))
expectParse("하루 뒤", .set(d(2026, 10, 1)))
expectParse("이틀 후", .set(d(2026, 10, 2)))
expectParse("열흘 뒤", .set(d(2026, 10, 10)))

// Moves from each reminder's own day
expectParse("+1", .shift(1))
expectParse("+2주", .shift(14))
expectParse("-1", .shift(-1))
expectParse("+3일", .shift(3))

// Weekdays (Monday-first weeks; today is Wednesday 9/30)
expectParse("금요일", .set(d(2026, 10, 2)))
expectParse("금", .set(d(2026, 10, 2)))
expectParse("금욜", .set(d(2026, 10, 2)))
expectParse("수요일", .set(d(2026, 10, 7)), "today's weekday means next week")
expectParse("일요일", .set(d(2026, 10, 4)))
expectParse("월요일", .set(d(2026, 10, 5)))
expectParse("다음 월요일", .set(d(2026, 10, 5)))
expectParse("이번 주 금요일", .set(d(2026, 10, 2)))
expectParse("이번주 월요일", .set(d(2026, 9, 28)))
expectParse("금주 목요일", .set(d(2026, 10, 1)))
expectParse("다음 주 금요일", .set(d(2026, 10, 9)))
expectParse("다음주금", .set(d(2026, 10, 9)))
expectParse("담주 수", .set(d(2026, 10, 7)))
expectParse("다다음주 월", .set(d(2026, 10, 12)))
expectParse("지난주 금요일", .set(d(2026, 9, 25)))
expectParse("금요일에", .set(d(2026, 10, 2)))
expectParse("다음 주", .set(d(2026, 10, 5)))
expectParse("다다음 주", .set(d(2026, 10, 12)))
expectFailure("이번 주")
expectFailure("금주")
expectParse("금주 금", .set(d(2026, 10, 2)))

// Weekends
expectParse("주말", .set(d(2026, 10, 3)))
expectParse("이번 주말", .set(d(2026, 10, 3)))
expectParse("다음 주말", .set(d(2026, 10, 10)))
expectParse("다음주말", .set(d(2026, 10, 10)))
expectParse("다음 주 주말", .set(d(2026, 10, 10)))

// Month-relative
expectParse("월말", .set(d(2026, 9, 30)))
expectParse("이번 달 말", .set(d(2026, 9, 30)))
expectParse("다음 달 말", .set(d(2026, 10, 31)))
expectParse("다음달 15일", .set(d(2026, 10, 15)))
expectParse("다음 달", .set(d(2026, 10, 30)))
expectParse("이번 달 5일", .set(d(2026, 9, 5)))
expectFailure("이번 달")
expectParse("다음 달 31일", .set(d(2026, 10, 31)))
expectFailure("다다음 달 31일")

// Explicit dates
expectParse("10/15", .set(d(2026, 10, 15)))
expectParse("10.15", .set(d(2026, 10, 15)))
expectParse("10-15", .set(d(2026, 10, 15)))
expectParse("10월 15일", .set(d(2026, 10, 15)))
expectParse("10월15일", .set(d(2026, 10, 15)))
expectParse("10월 15일에", .set(d(2026, 10, 15)))
expectParse("9/30", .set(d(2026, 9, 30)), "today")
expectParse("9/29", .set(d(2027, 9, 29)), "a passed date means next year")
expectParse("1/5", .set(d(2027, 1, 5)))
expectParse("2/29", .set(d(2028, 2, 29)), "next leap year")
expectParse("2027-03-01", .set(d(2027, 3, 1)))
expectParse("2026.12.25", .set(d(2026, 12, 25)))
expectParse("2026년 12월 25일", .set(d(2026, 12, 25)))
expectParse("내년 3월 1일", .set(d(2027, 3, 1)))
expectParse("올해 12월 25일", .set(d(2026, 12, 25)))
expectParse("15일", .set(d(2026, 10, 15)), "the next 15th")
expectParse("30일", .set(d(2026, 9, 30)))
expectParse("31일", .set(d(2026, 10, 31)), "September has no 31st")
expectParse("5일까지", .set(d(2026, 10, 5)))
expectFailure("2/30")
expectFailure("13/40")
expectFailure("2026-02-30")

// Times keep each reminder's own day
expectParse("오후 3시", .keep, .set(t(15)))
expectParse("오후3시", .keep, .set(t(15)))
expectParse("오후 3시 반", .keep, .set(t(15, 30)))
expectParse("오후3시반", .keep, .set(t(15, 30)))
expectParse("3시", .keep, .set(t(15)), "bare 1–6시 are afternoon")
expectParse("9시", .keep, .set(t(9)))
expectParse("12시", .keep, .set(t(12)))
expectParse("오전 12시", .keep, .set(t(0)))
expectParse("오전 9시 30분", .keep, .set(t(9, 30)))
expectParse("15:30", .keep, .set(t(15, 30)))
expectParse("9:05", .keep, .set(t(9, 5)))
expectParse("3:30", .keep, .set(t(15, 30)), "a single-digit 1–6 is the afternoon, like 3시")
expectParse("03:30", .keep, .set(t(3, 30)), "zero-padded is literal")
expectParse("06:05", .keep, .set(t(6, 5)))
expectParse("0:15", .keep, .set(t(0, 15)))
expectParse("7:45", .keep, .set(t(7, 45)))
expectParse("23:59", .keep, .set(t(23, 59)))
expectParse("오후 3:30", .keep, .set(t(15, 30)))
expectParse("저녁 7시 30분", .keep, .set(t(19, 30)))
expectParse("밤 11시", .keep, .set(t(23)))
expectParse("낮 2시", .keep, .set(t(14)))
expectParse("점심 12시", .keep, .set(t(12)))
expectParse("아침 8시", .keep, .set(t(8)))
expectParse("새벽 2시", .keep, .set(t(2)))
expectParse("정오", .keep, .set(t(12)))
expectParse("자정", .shift(1), .set(t(0)))
expectParse("밤 12시", .shift(1), .set(t(0)))
expectParse("밤 2시", .shift(1), .set(t(2)))
expectParse("세시", .keep, .set(t(15)))
expectParse("세 시 반", .keep, .set(t(15, 30)))
expectParse("다섯시 반", .keep, .set(t(17, 30)))
expectParse("열한시", .keep, .set(t(11)))
expectParse("오후", .keep, .set(t(15)))
expectParse("3시쯤", .keep, .set(t(15)))
expectParse("종일", .keep, .clear)
expectParse("하루 종일", .keep, .clear)
expectParse("시간 없음", .keep, .clear)
expectFailure("25시")
expectFailure("오후 3시 70분")
expectFailure("24:00")
expectFailure("3시간")

// Day and time together, in either order
expectParse("내일 오후 3시", .set(d(2026, 10, 1)), .set(t(15)))
expectParse("내일 아침", .set(d(2026, 10, 1)), .set(t(8)))
expectParse("오늘 저녁", .set(today), .set(t(19)))
expectParse("다음 주 금 오후 3시 30분", .set(d(2026, 10, 9)), .set(t(15, 30)))
expectParse("10/15 9:00", .set(d(2026, 10, 15)), .set(t(9)))
expectParse("10월 15일 오후 2시", .set(d(2026, 10, 15)), .set(t(14)))
expectParse("오후 3시 내일", .set(d(2026, 10, 1)), .set(t(15)))
expectParse("금요일 오후 6시까지", .set(d(2026, 10, 2)), .set(t(18)))
expectParse("내일 종일", .set(d(2026, 10, 1)), .clear)
expectParse("모레 밤 12시", .set(d(2026, 10, 3)), .set(t(0)), "midnight after 모레")
expectParse("오늘 밤 2시", .set(d(2026, 10, 1)), .set(t(2)))
expectParse("+1 오전 9시", .shift(1), .set(t(9)))

// From now
expectParse("2시간 후", .set(today), .set(t(20)))
expectParse("두 시간 뒤", .set(today), .set(t(20)))
expectParse("30분 뒤", .set(today), .set(t(18, 30)))
expectParse("8시간 후", .set(d(2026, 10, 1)), .set(t(2)))
expectParse("1시간 30분 후", .set(today), .set(t(19, 30)))
expectParse("1시간 반 뒤", .set(today), .set(t(19, 30)))
expectFailure("내일 2시간 후")

// Removing the date
expectEqual(parse("없음"), .edit(.clear))
expectEqual(parse("날짜 없음"), .edit(.clear))
expectEqual(parse("삭제"), .edit(.clear))
expectEqual(parse("clear"), .edit(.clear))

// Nonsense
expectFailure("아무거나")
expectFailure("내일 아무거나")
expectFailure("에")

// MARK: Labels

expectEqual(dayLabel(today, today: today, calendar: calendar), "오늘")
expectEqual(dayLabel(d(2026, 10, 1), today: today, calendar: calendar), "내일")
expectEqual(dayLabel(d(2026, 10, 2), today: today, calendar: calendar), "모레")
expectEqual(dayLabel(d(2026, 9, 29), today: today, calendar: calendar), "어제")
expectEqual(dayLabel(d(2026, 10, 9), today: today, calendar: calendar), "10월 9일 (금)")
expectEqual(dayLabel(d(2027, 1, 5), today: today, calendar: calendar), "2027년 1월 5일 (화)")
expectEqual(timeLabel(t(15)), "오후 3:00")
expectEqual(timeLabel(t(9, 5)), "오전 9:05")
expectEqual(timeLabel(t(12)), "오후 12:00")
expectEqual(timeLabel(t(0)), "오전 12:00")
expectEqual(timeLabel(t(23, 59)), "오후 11:59")
expectEqual(dueLabel(nil, today: today, calendar: calendar), "날짜 없음")
expectEqual(dueLabel(tomorrowAtThree, today: today, calendar: calendar), "내일 오후 3:00")
expectEqual(fullDueLabel(tomorrowAtThree, today: today, calendar: calendar), "내일 · 10월 1일 (목) 오후 3:00")
expectEqual(fullDueLabel(DueValue(d(2026, 10, 9)), today: today, calendar: calendar), "10월 9일 (금)")
expectEqual(monthTitle(d(2026, 10, 1)), "2026년 10월")
expectEqual(describeEdit(DueEdit(day: .set(d(2026, 10, 2))), today: today, calendar: calendar), "모레 · 10월 2일 (금)")
expectEqual(describeEdit(DueEdit(day: .set(d(2026, 10, 9)), time: .set(t(15))), today: today, calendar: calendar), "10월 9일 (금) 오후 3:00")
expectEqual(describeEdit(DueEdit(day: .shift(1)), today: today, calendar: calendar), "하루 미루기")
expectEqual(describeEdit(DueEdit(day: .shift(7)), today: today, calendar: calendar), "일주일 미루기")
expectEqual(describeEdit(DueEdit(day: .shift(-3)), today: today, calendar: calendar), "3일 당기기")
expectEqual(describeEdit(DueEdit(time: .set(t(15))), today: today, calendar: calendar), "오후 3:00")
expectEqual(describeEdit(DueEdit(time: .clear), today: today, calendar: calendar), "종일")
expectEqual(describeEdit(.clear, today: today, calendar: calendar), "날짜 없음")

// MARK: Month grid

let october = monthGrid(year: 2026, month: 10, firstWeekday: 1, calendar: calendar)
expectEqual(october.count, 42)
expectEqual(october.first, d(2026, 9, 27), "Sunday-first grid starts on Sunday 9/27")
expectEqual(october[4], d(2026, 10, 1), "10/1 is a Thursday")
expectEqual(october.last, d(2026, 11, 7))
let octoberMondayFirst = monthGrid(year: 2026, month: 10, firstWeekday: 2, calendar: calendar)
expectEqual(octoberMondayFirst.first, d(2026, 9, 28))
expectEqual(octoberMondayFirst[3], d(2026, 10, 1))
let february = monthGrid(year: 2026, month: 2, firstWeekday: 1, calendar: calendar)
expectEqual(february.first, d(2026, 2, 1), "Feb 2026 starts on a Sunday")
expectEqual(weekdayHeader(firstWeekday: 1).map(\.symbol), ["일", "월", "화", "수", "목", "금", "토"])
expectEqual(weekdayHeader(firstWeekday: 2).map(\.symbol), ["월", "화", "수", "목", "금", "토", "일"])
expectEqual(weekdayHeader(firstWeekday: 2).last?.weekday, 1)

// MARK: Buckets and ordering

expectEqual(bucket(for: nil, now: now, calendar: calendar), .undated)
expectEqual(bucket(for: DueValue(d(2026, 9, 29)), now: now, calendar: calendar), .overdue)
expectEqual(bucket(for: DueValue(today), now: now, calendar: calendar), .today)
expectEqual(bucket(for: DueValue(today, t(9)), now: now, calendar: calendar), .overdue, "a passed time today is overdue")
expectEqual(bucket(for: DueValue(today, t(20)), now: now, calendar: calendar), .today)
expectEqual(bucket(for: DueValue(d(2026, 10, 1)), now: now, calendar: calendar), .tomorrow)
expectEqual(bucket(for: DueValue(d(2026, 10, 7)), now: now, calendar: calendar), .thisWeek)
expectEqual(bucket(for: DueValue(d(2026, 10, 8)), now: now, calendar: calendar), .later)
expectEqual(dueSortsBefore(DueValue(d(2026, 10, 1)), DueValue(d(2026, 10, 1), t(9))), true, "all-day first")
expectEqual(dueSortsBefore(DueValue(d(2026, 10, 1), t(9)), DueValue(d(2026, 10, 2))), true)
expectEqual(dueSortsBefore(nil, DueValue(d(2026, 10, 2))), false, "undated last")
expectEqual(dueSortsBefore(DueValue(d(2026, 10, 2)), nil), true)
expectEqual(dueSortsBefore(DueValue(d(2026, 10, 2)), DueValue(d(2026, 10, 2))), nil)
expectEqual(dueSortsBefore(nil, nil), nil)

// MARK: EventKit components

let allDayComponents = DueValue(d(2026, 10, 1)).components(calendar: calendar, previous: nil)
expectEqual(allDayComponents.year, 2026)
expectEqual(allDayComponents.month, 10)
expectEqual(allDayComponents.day, 1)
expectEqual(allDayComponents.hour, nil, "all-day components carry no hour")
expectEqual(allDayComponents.timeZone, nil)
let floatingComponents = tomorrowAtThree.components(calendar: calendar, previous: nil)
expectEqual(floatingComponents.hour, 15)
expectEqual(floatingComponents.minute, 0)
expectEqual(floatingComponents.second, 0)
expectEqual(floatingComponents.timeZone, nil, "new timed values stay floating, like Reminders' own")
var pinnedPrevious = DateComponents(year: 2026, month: 9, day: 1, hour: 1)
pinnedPrevious.timeZone = TimeZone(identifier: "UTC")
expectEqual(tomorrowAtThree.components(calendar: calendar, previous: pinnedPrevious).timeZone, calendar.timeZone,
            "a pinned value is re-pinned to the local zone")
expectEqual(DueValue(components: DateComponents(year: 2026, month: 10, day: 1, hour: 15, minute: 0), calendar: calendar),
            tomorrowAtThree)
expectEqual(DueValue(components: DateComponents(year: 2026, month: 10, day: 1), calendar: calendar),
            DueValue(d(2026, 10, 1)))
var utcComponents = DateComponents(year: 2026, month: 10, day: 1, hour: 6, minute: 0)
utcComponents.timeZone = TimeZone(identifier: "UTC")
expectEqual(DueValue(components: utcComponents, calendar: calendar), tomorrowAtThree, "06:00 UTC is 15:00 in Seoul")
expectEqual(DueValue(components: DateComponents(month: 10, day: 1), calendar: calendar), nil, "no year is unreadable")
expectEqual(DueValue(components: nil, calendar: calendar), nil)
expectEqual(DueValue(components: DateComponents(year: 2026, month: 2, day: 30), calendar: calendar), nil)
expectEqual(tomorrowAtThree.instant(calendar), at(2026, 10, 1, 15, 0))
expectEqual(DueValue(d(2026, 10, 1)).instant(calendar), at(2026, 10, 1))

// MARK: Alarms follow the due date

func plan(_ old: DueValue?, _ new: DueValue?, _ alarms: [Date]) -> AlarmPlan {
    planAlarms(oldDue: old, newDue: new, absoluteAlarms: alarms, calendar: calendar)
}

expectEqual(plan(tomorrowAtThree, DueValue(d(2026, 10, 2), t(15)), [at(2026, 10, 1, 15)]),
            AlarmPlan(remove: [0], add: [at(2026, 10, 2, 15)]), "timed → timed moves the due alarm")
expectEqual(plan(tomorrowAtThree, DueValue(d(2026, 10, 2), t(15)), [at(2026, 10, 1, 15), at(2026, 9, 28, 10)]),
            AlarmPlan(remove: [0], add: [at(2026, 10, 2, 15)]), "an unrelated alarm stays")
expectEqual(plan(tomorrowAtThree, DueValue(d(2026, 10, 2), t(15)), [at(2026, 9, 30, 9)]),
            AlarmPlan(), "no due alarm before means none after")
expectEqual(plan(tomorrowAtThree, DueValue(d(2026, 10, 1)), [at(2026, 10, 1, 15), at(2026, 9, 30, 9)]),
            AlarmPlan(remove: [0], add: []), "timed → all-day removes the due alarm")
expectEqual(plan(tomorrowAtThree, nil, [at(2026, 10, 1, 15)]),
            AlarmPlan(remove: [0], add: []), "removing the date removes the due alarm")
expectEqual(plan(DueValue(d(2026, 10, 1)), DueValue(d(2026, 10, 1), t(15)), []),
            AlarmPlan(remove: [], add: [at(2026, 10, 1, 15)]), "adding a time adds its alarm")
expectEqual(plan(DueValue(d(2026, 10, 1)), DueValue(d(2026, 10, 5)), [at(2026, 10, 1, 9)]),
            AlarmPlan(remove: [0], add: [at(2026, 10, 5, 9)]), "all-day → all-day moves a day alarm by the same days")
expectEqual(plan(nil, DueValue(d(2026, 10, 1), t(15)), []),
            AlarmPlan(remove: [], add: [at(2026, 10, 1, 15)]))
expectEqual(plan(nil, DueValue(d(2026, 10, 1)), []), AlarmPlan())
expectEqual(plan(DueValue(d(2026, 10, 1)), DueValue(d(2026, 10, 2), t(15)), [at(2026, 10, 2, 15)]),
            AlarmPlan(), "no duplicate alarm is added")
expectEqual(plan(tomorrowAtThree, tomorrowAtThree, [at(2026, 10, 1, 15)]), AlarmPlan(), "unchanged")
expectEqual(plan(tomorrowAtThree, DueValue(d(2026, 10, 1), t(16)), [at(2026, 10, 1, 15, 0).addingTimeInterval(30)]),
            AlarmPlan(remove: [0], add: [at(2026, 10, 1, 16)]), "within a minute counts as the due alarm")

// MARK: Start date follows or stays

func start(_ start: DueValue?, _ old: DueValue?, _ new: DueValue?) -> StartPlan {
    planStart(start: start, oldDue: old, newDue: new, calendar: calendar)
}

expectEqual(start(nil, tomorrowAtThree, DueValue(d(2026, 10, 3))), .keep)
expectEqual(start(DueValue(d(2026, 10, 1), t(10)), tomorrowAtThree, nil), .clear)
expectEqual(start(DueValue(d(2026, 10, 1), t(0)), DueValue(d(2026, 10, 1)), DueValue(d(2026, 10, 5))),
            .set(DueValue(d(2026, 10, 5))), "a start that mirrored the due day follows it")
expectEqual(start(DueValue(d(2026, 10, 1), t(10)), tomorrowAtThree, DueValue(d(2026, 10, 3), t(15))), .keep)
expectEqual(start(DueValue(d(2026, 10, 1), t(10)), tomorrowAtThree, DueValue(d(2026, 9, 30), t(15))),
            .set(DueValue(d(2026, 9, 30), t(15))), "a start after the new due is pulled back")
expectEqual(start(DueValue(d(2026, 10, 1), t(10)), DueValue(d(2026, 10, 2)), DueValue(d(2026, 10, 1))), .keep,
            "a start on the new all-day due day is not after it")
expectEqual(start(tomorrowAtThree, tomorrowAtThree, DueValue(d(2026, 10, 4), t(9))),
            .set(DueValue(d(2026, 10, 4), t(9))), "a start equal to the due instant follows it")

let midnightStart = DateComponents(year: 2026, month: 10, day: 1, hour: 0, minute: 0)
let movedMidnight = startComponents(for: DueValue(d(2026, 10, 5)), previous: midnightStart, calendar: calendar)
expectEqual([movedMidnight.year, movedMidnight.month, movedMidnight.day, movedMidnight.hour, movedMidnight.minute],
            [2026, 10, 5, 0, 0], "an all-day start stored at 00:00 keeps 00:00")
let movedDateOnly = startComponents(for: DueValue(d(2026, 10, 5)), previous: DateComponents(year: 2026, month: 10, day: 1), calendar: calendar)
expectEqual(movedDateOnly.hour, nil, "a date-only start stays date-only")
let movedTimed = startComponents(for: DueValue(d(2026, 10, 5), t(9, 30)), previous: midnightStart, calendar: calendar)
expectEqual([movedTimed.hour, movedTimed.minute], [9, 30])
expectEqual(movedTimed.timeZone, nil)
var pinnedStart = DateComponents(year: 2026, month: 10, day: 1, hour: 1, minute: 0)
pinnedStart.timeZone = TimeZone(identifier: "UTC")
let movedPinned = startComponents(for: DueValue(d(2026, 10, 5)), previous: pinnedStart, calendar: calendar)
expectEqual([movedPinned.hour, movedPinned.minute], [10, 0], "01:00 UTC is 10:00 in Seoul")
expectEqual(movedPinned.timeZone, calendar.timeZone)

print("DueCore tests: \(results.passed) passed, \(results.failed) failed")
exit(results.failed == 0 ? 0 : 1)
