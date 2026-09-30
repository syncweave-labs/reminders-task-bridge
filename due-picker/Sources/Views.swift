// Views.swift — the window: lists on the left, reminders in the middle, the
// date panel on the right.

import AppKit
import SwiftUI

extension RGBColor {
    var color: Color { Color(.sRGB, red: red, green: green, blue: blue, opacity: 1) }
}

struct ContentView: View {
    @ObservedObject var model: AppModel

    var body: some View {
        Group {
            switch model.access {
            case .granted:
                MainView(model: model)
            case .checking, .notDetermined:
                ProgressView("미리알림 권한을 확인하는 중…")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            case .denied, .restricted, .failed:
                AccessHelpView(model: model)
            }
        }
        .frame(minWidth: 1040, minHeight: 660)
        .task { await model.start() }
    }
}

/// Hover and other view-local flags. `@State` is a macro in current SDKs and
/// its plugin ships only with Xcode, so this build (Command Line Tools) keeps
/// view-local state in a small object instead.
final class HoverState: ObservableObject {
    @Published var isHovering = false
}

struct MainView: View {
    @ObservedObject var model: AppModel
    @FocusState private var focus: FocusTarget?

    var body: some View {
        NavigationSplitView {
            SidebarView(model: model)
                .navigationSplitViewColumnWidth(min: 190, ideal: 220, max: 280)
        } detail: {
            ReminderListView(model: model, focus: $focus)
                .inspector(isPresented: $model.showsInspector) {
                    InspectorView(model: model, focus: $focus)
                        .inspectorColumnWidth(min: 360, ideal: 390, max: 480)
                }
        }
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button {
                    Task { await model.reload() }
                } label: {
                    Label("새로 고침", systemImage: "arrow.clockwise")
                }
                .help("새로 고침 (⌘R)")
            }
            ToolbarItem(placement: .primaryAction) {
                Button {
                    model.showsInspector.toggle()
                } label: {
                    Label("날짜 패널", systemImage: "sidebar.trailing")
                }
                .help("날짜 패널 보이기/숨기기")
            }
        }
        .onChange(of: model.focusRequest) { _, request in
            guard let request else { return }
            if request == .dateInput { model.showsInspector = true }
            focus = request
            model.focusRequest = nil
        }
    }
}

// MARK: - Sidebar

struct SidebarView: View {
    @ObservedObject var model: AppModel
    /// Snapshots only: the vibrant sidebar style does not draw offscreen.
    var plainStyle = false

    private var filterBinding: Binding<ReminderFilter?> {
        Binding(get: { model.filter }, set: { model.filter = $0 ?? .all })
    }

    var body: some View {
        if plainStyle {
            content.listStyle(.inset)
        } else {
            content.listStyle(.sidebar)
        }
    }

    private var content: some View {
        List(selection: filterBinding) {
            Section("스마트 목록") {
                SidebarRow(title: "전체", systemImage: "tray.full.fill", tint: .gray,
                           count: model.count(for: .all))
                    .tag(ReminderFilter.all)
                SidebarRow(title: "오늘", systemImage: "sun.max.fill", tint: .blue,
                           count: model.count(for: .today))
                    .tag(ReminderFilter.today)
                SidebarRow(title: "지연됨", systemImage: "exclamationmark.circle.fill", tint: .red,
                           count: model.count(for: .overdue))
                    .tag(ReminderFilter.overdue)
                SidebarRow(title: "날짜 있음", systemImage: "calendar", tint: .orange,
                           count: model.count(for: .scheduled))
                    .tag(ReminderFilter.scheduled)
                SidebarRow(title: "날짜 없음", systemImage: "calendar.badge.minus", tint: .secondary,
                           count: model.count(for: .undated))
                    .tag(ReminderFilter.undated)
            }
            Section("목록") {
                ForEach(model.lists) { list in
                    SidebarRow(title: list.title, systemImage: "list.bullet.circle.fill", tint: list.color.color,
                               count: model.count(for: .list(list.id)), note: list.isEditable ? nil : "읽기 전용")
                        .tag(ReminderFilter.list(list.id))
                }
            }
        }
    }
}

struct SidebarRow: View {
    let title: String
    let systemImage: String
    let tint: Color
    let count: Int
    var note: String?

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: systemImage)
                .foregroundStyle(tint)
                .frame(width: 18)
            Text(title)
                .lineLimit(1)
            if let note {
                Text(note)
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }
            Spacer(minLength: 4)
            Text("\(count)")
                .font(.callout)
                .monospacedDigit()
                .foregroundStyle(.secondary)
        }
        .accessibilityElement(children: .combine)
    }
}

// MARK: - Reminder list

struct ReminderListView: View {
    @ObservedObject var model: AppModel
    var focus: FocusState<FocusTarget?>.Binding

    var body: some View {
        let sections = model.sections
        let visibleCount = model.visibleItems.count
        let selectedCount = model.selectedItems.count
        List(selection: $model.selection) {
            ForEach(sections) { section in
                Section {
                    ForEach(section.items) { item in
                        ReminderRowView(item: item, list: model.list(for: item),
                                        label: model.label(for: item), bucket: section.bucket)
                            .tag(item.id)
                    }
                } header: {
                    HStack {
                        Text(section.bucket.title)
                        Spacer()
                        Text("\(section.items.count)")
                            .monospacedDigit()
                    }
                    .foregroundStyle(section.bucket == .overdue ? Color.red : Color.secondary)
                }
            }
        }
        .listStyle(.inset)
        .contextMenu(forSelectionType: String.self) { ids in
            if !ids.isEmpty {
                Button("오늘") { apply(.today, to: ids) }
                Button("내일") { apply(.tomorrow, to: ids) }
                Button("모레") { apply(.dayAfterTomorrow, to: ids) }
                Button("이번 주말") { apply(.thisWeekend, to: ids) }
                Button("다음 주 월요일") { apply(.nextMonday, to: ids) }
                Divider()
                Button("하루 미루기") { apply(.postponeDay, to: ids) }
                Button("일주일 미루기") { apply(.postponeWeek, to: ids) }
                Divider()
                Button("날짜 없음") { apply(DuePreset.clear, to: ids) }
            }
        } primaryAction: { ids in
            if !ids.isEmpty {
                model.selection = ids
                model.focusRequest = .dateInput
            }
        }
        .overlay {
            if model.loadedOnce && sections.isEmpty {
                if model.search.trimmingCharacters(in: .whitespaces).isEmpty {
                    ContentUnavailableView("미완료 미리알림이 없어요", systemImage: "checkmark.circle",
                                           description: Text("이 목록은 모두 끝났어요."))
                } else {
                    ContentUnavailableView.search(text: model.search)
                }
            } else if !model.loadedOnce {
                ProgressView()
            }
        }
        .safeAreaInset(edge: .bottom, spacing: 0) {
            QuickAddBar(model: model, focus: focus)
        }
        .searchable(text: $model.search, placement: .toolbar, prompt: "제목 검색")
        .navigationTitle(model.filterTitle)
        .navigationSubtitle(selectedCount == 0 ? "\(visibleCount)개" : "\(visibleCount)개 · \(selectedCount)개 선택")
    }

    private func apply(_ preset: DuePreset, to ids: Set<String>) {
        model.selection = ids
        model.apply(preset)
    }
}

struct ReminderRowView: View {
    let item: ReminderItem
    let list: ReminderList?
    let label: String
    let bucket: DueBucket

    var body: some View {
        HStack(alignment: .center, spacing: 10) {
            Circle()
                .fill((list?.color ?? .fallback).color)
                .frame(width: 9, height: 9)
            VStack(alignment: .leading, spacing: 2) {
                Text(item.displayTitle)
                    .lineLimit(1)
                HStack(spacing: 5) {
                    Text(list?.title ?? "")
                    if item.isRecurring { Image(systemName: "repeat").help("반복") }
                    if item.hasAlarms { Image(systemName: "bell").help("알림 있음") }
                    if !item.isEditable { Image(systemName: "lock").help("읽기 전용 목록") }
                }
                .font(.caption)
                .foregroundStyle(.secondary)
                .lineLimit(1)
            }
            Spacer(minLength: 8)
            Text(label)
                .font(.callout)
                .monospacedDigit()
                .foregroundStyle(dueColor)
                .lineLimit(1)
        }
        .padding(.vertical, 3)
        .contentShape(Rectangle())
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(item.displayTitle), \(list?.title ?? ""), \(label)")
    }

    private var dueColor: Color {
        switch bucket {
        case .overdue: return .red
        case .today: return .blue
        case .undated: return .secondary
        case .tomorrow, .thisWeek, .later: return .primary
        }
    }
}

struct QuickAddBar: View {
    @ObservedObject var model: AppModel
    var focus: FocusState<FocusTarget?>.Binding

    private var listBinding: Binding<String?> {
        Binding(get: { model.quickAddTargetListID }, set: { model.quickAddListID = $0 })
    }

    private var canAdd: Bool { !model.quickAddTitle.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 8) {
                Image(systemName: "plus.circle.fill")
                    .font(.title3)
                    .foregroundStyle(.tint)
                    .accessibilityHidden(true)
                TextField("새 미리알림 (⌘N)", text: $model.quickAddTitle)
                    .textFieldStyle(.plain)
                    .focused(focus, equals: .quickAddTitle)
                    .onSubmit { model.submitQuickAdd() }
                TextField("날짜 (예: 내일 3시)", text: $model.quickAddDate)
                    .textFieldStyle(.roundedBorder)
                    .frame(width: 150)
                    .onSubmit { model.submitQuickAdd() }
                    .accessibilityLabel("새 미리알림 날짜")
                Picker("목록", selection: listBinding) {
                    ForEach(model.editableLists) { list in
                        Text(list.title).tag(Optional(list.id))
                    }
                }
                .labelsHidden()
                .frame(width: 120)
                Button("추가") { model.submitQuickAdd() }
                    .disabled(!canAdd)
            }
            if let preview = model.quickAddPreview {
                Text(preview.isError ? preview.text : "→ \(preview.text)")
                    .font(.caption)
                    .foregroundStyle(preview.isError ? Color.red : Color.secondary)
                    .padding(.leading, 30)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 9)
        .background(.bar)
        .overlay(alignment: .top) { Divider() }
    }
}

// MARK: - Date panel

struct InspectorView: View {
    @ObservedObject var model: AppModel
    var focus: FocusState<FocusTarget?>.Binding

    var body: some View {
        VStack(spacing: 0) {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    SelectionSummary(model: model)
                    DateInputSection(model: model, focus: focus)
                    PresetSection(model: model)
                    TimeSection(model: model)
                    MonthCalendarView(model: model)
                }
                .padding(16)
            }
            StatusBar(model: model)
        }
    }
}

struct SectionTitle: View {
    let title: String
    let systemImage: String

    var body: some View {
        Label(title, systemImage: systemImage)
            .font(.subheadline.weight(.semibold))
            .foregroundStyle(.secondary)
    }
}

struct SelectionSummary: View {
    @ObservedObject var model: AppModel

    var body: some View {
        let selected = model.selectedItems
        VStack(alignment: .leading, spacing: 5) {
            if selected.isEmpty {
                Text("미리알림을 고르세요")
                    .font(.title3.weight(.semibold))
                Text("⌘-클릭이나 ⇧-클릭으로 여러 개를 골라 한 번에 옮길 수 있어요.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            } else if selected.count == 1, let item = selected.first {
                Text(item.displayTitle)
                    .font(.title3.weight(.semibold))
                    .lineLimit(2)
                    .textSelection(.enabled)
                Label(item.hasUnreadableDue ? "날짜를 읽을 수 없어요" : fullDueLabel(item.due, today: model.today, calendar: model.calendar),
                      systemImage: "calendar")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                HStack(spacing: 10) {
                    if let list = model.list(for: item) {
                        Label(list.title, systemImage: "circle.fill")
                            .foregroundStyle(list.color.color)
                    }
                    if item.isRecurring { Label("반복", systemImage: "repeat") }
                    if item.hasAlarms { Label("알림", systemImage: "bell") }
                }
                .font(.caption)
                .foregroundStyle(.secondary)
            } else {
                Text("\(selected.count)개 선택됨")
                    .font(.title3.weight(.semibold))
                Text(groupSummary(selected))
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
                Text("각자의 시간은 그대로 두고 날짜만, 또는 날짜는 그대로 두고 시간만 바꿀 수 있어요.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func groupSummary(_ items: [ReminderItem]) -> String {
        var order: [String] = []
        var counts: [String: Int] = [:]
        for item in items {
            let label = item.due.map { dayLabel($0.day, today: model.today, calendar: model.calendar) } ?? "날짜 없음"
            if counts[label] == nil { order.append(label) }
            counts[label, default: 0] += 1
        }
        return order.map { "\($0) \(counts[$0] ?? 0)" }.joined(separator: " · ")
    }
}

struct DateInputSection: View {
    @ObservedObject var model: AppModel
    var focus: FocusState<FocusTarget?>.Binding

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            SectionTitle(title: "말로 입력", systemImage: "keyboard")
            TextField("내일 3시, 다음 주 금, 10/15, 3일 후…", text: $model.dateInput)
                .textFieldStyle(.roundedBorder)
                .controlSize(.large)
                .focused(focus, equals: .dateInput)
                .onSubmit { model.submitDateInput() }
                .accessibilityLabel("날짜를 말로 입력")
            if let preview = model.dateInputPreview {
                Text(preview.text)
                    .font(.callout.weight(.medium))
                    .foregroundStyle(preview.isError ? Color.red : Color.accentColor)
            } else {
                Text("Enter로 적용 · ⌘L로 바로 입력 · ‘종일’, ‘없음’도 돼요")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }
}

struct TileButtonStyle: ButtonStyle {
    var isHighlighted = false

    func makeBody(configuration: Configuration) -> some View {
        TileBody(configuration: configuration, isHighlighted: isHighlighted)
    }

    private struct TileBody: View {
        let configuration: ButtonStyleConfiguration
        let isHighlighted: Bool
        @Environment(\.isEnabled) private var isEnabled
        @StateObject private var hover = HoverState()

        var body: some View {
            let shape = RoundedRectangle(cornerRadius: 8, style: .continuous)
            configuration.label
                .padding(.horizontal, 10)
                .padding(.vertical, 5)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(shape.fill(fill))
                .overlay(shape.strokeBorder(isHighlighted ? Color.accentColor.opacity(0.7) : Color.primary.opacity(0.08)))
                .contentShape(shape)
                .opacity(isEnabled ? 1 : 0.45)
                .onHover { hover.isHovering = $0 }
        }

        private var fill: Color {
            if isHighlighted { return Color.accentColor.opacity(configuration.isPressed ? 0.3 : 0.16) }
            if configuration.isPressed { return Color.primary.opacity(0.16) }
            return Color.primary.opacity(hover.isHovering && isEnabled ? 0.1 : 0.05)
        }
    }
}

struct PresetSection: View {
    @ObservedObject var model: AppModel

    private let layout: [(DuePreset, String)] = [
        (.today, "⌘1"), (.tomorrow, "⌘2"), (.dayAfterTomorrow, "⌘3"),
        (.thisWeekend, "⌘4"), (.nextMonday, "⌘5"), (.clear, "⌘0"),
        (.advanceDay, "⌘["), (.postponeDay, "⌘]"), (.postponeWeek, "⇧⌘]"),
    ]

    var body: some View {
        let selected = model.selectedItems
        let singleDay: Day? = selected.count == 1 ? selected.first?.due?.day : nil
        let noSelection = selected.isEmpty
        VStack(alignment: .leading, spacing: 8) {
            SectionTitle(title: "빠른 선택", systemImage: "bolt")
            LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: 6), count: 3), spacing: 6) {
                ForEach(layout, id: \.0) { preset, shortcut in
                    let target = preset.targetDay(today: model.today, calendar: model.calendar)
                    Button {
                        model.apply(preset)
                    } label: {
                        VStack(alignment: .leading, spacing: 1) {
                            Text(preset.title)
                                .font(.callout.weight(.medium))
                                .lineLimit(1)
                                .minimumScaleFactor(0.8)
                            Text(subtitle(for: preset, target: target))
                                .font(.caption)
                                .foregroundStyle(.secondary)
                                .lineLimit(1)
                                .minimumScaleFactor(0.8)
                        }
                    }
                    .buttonStyle(TileButtonStyle(isHighlighted: target != nil && target == singleDay))
                    .help("\(preset.title) (\(shortcut))")
                    .disabled(noSelection)
                }
            }
        }
    }

    private func subtitle(for preset: DuePreset, target: Day?) -> String {
        if let target {
            return "\(target.month)/\(target.day) (\(koreanWeekdaySymbols[target.weekday(model.calendar) - 1]))"
        }
        switch preset {
        case .postponeDay: return "각자 +1일"
        case .postponeWeek: return "각자 +7일"
        case .advanceDay: return "각자 −1일"
        default: return "날짜·알림 제거"
        }
    }
}

struct MonthCalendarView: View {
    @ObservedObject var model: AppModel

    var body: some View {
        let month = model.displayedMonth
        let calendar = model.calendar
        let today = model.today
        let days = monthGrid(year: month.year, month: month.month, firstWeekday: model.firstWeekday, calendar: calendar)
        let counts = model.dueCounts
        let selectedDays = model.selectedDays
        let noSelection = !model.hasSelection
        VStack(spacing: 6) {
            HStack(spacing: 4) {
                SectionTitle(title: monthTitle(month), systemImage: "calendar")
                Spacer()
                Button { model.showMonth(offset: -1) } label: { Image(systemName: "chevron.left") }
                    .buttonStyle(.borderless)
                    .help("이전 달")
                    .accessibilityLabel("이전 달")
                Button("이번 달") { model.showCurrentMonth() }
                    .buttonStyle(.borderless)
                Button { model.showMonth(offset: 1) } label: { Image(systemName: "chevron.right") }
                    .buttonStyle(.borderless)
                    .help("다음 달")
                    .accessibilityLabel("다음 달")
            }
            HStack(spacing: 0) {
                ForEach(weekdayHeader(firstWeekday: model.firstWeekday), id: \.weekday) { symbol in
                    Text(symbol.symbol)
                        .font(.caption.weight(.medium))
                        .foregroundStyle(weekdayColor(symbol.weekday).opacity(0.85))
                        .frame(maxWidth: .infinity)
                }
            }
            LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: 0), count: 7), spacing: 2) {
                ForEach(days, id: \.self) { day in
                    let count = counts[day] ?? 0
                    DayCell(day: day, inMonth: day.month == month.month, isToday: day == today,
                            isSelected: selectedDays.contains(day), count: count,
                            weekday: day.weekday(calendar), isPast: day < today) {
                        model.pick(day: day)
                    }
                    .disabled(noSelection)
                    .help(fullDayLabel(day, today: today, calendar: calendar) + (count > 0 ? " · \(count)개 마감" : ""))
                }
            }
        }
        .help("날짜를 누르면 고른 미리알림이 각자의 시간을 지킨 채 그날로 옮겨져요. 점은 그날 마감인 미리알림 수예요.")
    }
}

func weekdayColor(_ weekday: Int) -> Color {
    switch weekday {
    case 1: return .red
    case 7: return .blue
    default: return .primary
    }
}

struct DayCell: View {
    let day: Day
    let inMonth: Bool
    let isToday: Bool
    let isSelected: Bool
    let count: Int
    let weekday: Int
    let isPast: Bool
    let action: () -> Void

    @Environment(\.isEnabled) private var isEnabled
    @StateObject private var hover = HoverState()

    var body: some View {
        Button(action: action) {
            VStack(spacing: 2) {
                Text("\(day.day)")
                    .font(.system(size: 13, weight: isToday || isSelected ? .semibold : .regular))
                    .monospacedDigit()
                    .foregroundStyle(numberColor)
                    .frame(width: 27, height: 27)
                    .background {
                        if isSelected {
                            Circle().fill(Color.accentColor)
                        } else if isToday {
                            Circle().strokeBorder(Color.accentColor, lineWidth: 1.5)
                        } else if hover.isHovering && isEnabled {
                            Circle().fill(Color.primary.opacity(0.1))
                        }
                    }
                LoadDots(count: count)
            }
            .frame(maxWidth: .infinity, minHeight: 33)
            .contentShape(Rectangle())
            .opacity(inMonth ? 1 : 0.35)
        }
        .buttonStyle(.plain)
        .onHover { hover.isHovering = $0 }
        .accessibilityLabel("\(day.month)월 \(day.day)일" + (count > 0 ? ", 마감 \(count)개" : ""))
    }

    private var numberColor: Color {
        if isSelected { return .white }
        if weekday == 1 { return .red.opacity(isPast ? 0.6 : 1) }
        if weekday == 7 { return .blue.opacity(isPast ? 0.6 : 1) }
        return isPast ? .secondary : .primary
    }
}

struct LoadDots: View {
    let count: Int

    var body: some View {
        HStack(spacing: 2) {
            ForEach(0..<min(count, 3), id: \.self) { _ in
                Circle().frame(width: 4, height: 4)
            }
        }
        .foregroundStyle(count > 3 ? Color.orange : Color.secondary)
        .frame(height: 4)
    }
}

struct TimeSection: View {
    @ObservedObject var model: AppModel

    private let chips: [(String, TimeOfDay?)] = [
        ("종일", nil), ("오전 9:00", TimeOfDay(9, 0)), ("정오", TimeOfDay(12, 0)),
        ("오후 3:00", TimeOfDay(15, 0)), ("오후 6:00", TimeOfDay(18, 0)), ("오후 9:00", TimeOfDay(21, 0)),
    ]

    var body: some View {
        let shared = model.sharedSelectedTime
        let noSelection = !model.hasSelection
        VStack(alignment: .leading, spacing: 8) {
            SectionTitle(title: "시간", systemImage: "clock")
            LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: 6), count: 3), spacing: 6) {
                ForEach(chips, id: \.0) { title, time in
                    Button {
                        model.pick(time: time)
                    } label: {
                        Text(title)
                            .font(.callout.weight(.medium))
                            .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(TileButtonStyle(isHighlighted: shared == .some(time)))
                    .disabled(noSelection)
                }
            }
            HStack(spacing: 8) {
                DatePicker("직접 입력", selection: $model.customTime, displayedComponents: .hourAndMinute)
                    .labelsHidden()
                    .datePickerStyle(.stepperField)
                Button("이 시간으로") { model.pickCustomTime() }
                    .disabled(noSelection)
                Spacer()
                Text("정한 시각에 알림")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .help("시간을 정하면 그 시각에 알림이 울리고, 종일로 바꾸면 그 알림은 없어져요. 날짜만 바꾸면 알림도 함께 옮겨져요.")
    }
}

struct StatusBar: View {
    @ObservedObject var model: AppModel

    var body: some View {
        HStack(spacing: 8) {
            if let status = model.status {
                Image(systemName: icon(for: status.tone))
                    .foregroundStyle(color(for: status.tone))
                    .accessibilityHidden(true)
                Text(status.text)
                    .font(.callout)
                    .lineLimit(2)
                    .truncationMode(.middle)
                    .textSelection(.enabled)
                Spacer(minLength: 4)
                if status.offersUndo && model.canUndo {
                    Button("되돌리기") { model.undoLast() }
                        .help("마지막 날짜 변경 되돌리기 (⌘Z)")
                }
            } else {
                Text("⌘1 오늘 · ⌘2 내일 · ⌘] 하루 미루기 · ⌘L 말로 입력 · ⌘Z 되돌리기")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                Spacer()
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .frame(maxWidth: .infinity, minHeight: 46)
        .background(.bar)
        .overlay(alignment: .top) { Divider() }
        .accessibilityElement(children: .contain)
    }

    private func icon(for tone: StatusMessage.Tone) -> String {
        switch tone {
        case .info: return "info.circle"
        case .success: return "checkmark.circle.fill"
        case .warning: return "exclamationmark.triangle.fill"
        case .error: return "xmark.octagon.fill"
        }
    }

    private func color(for tone: StatusMessage.Tone) -> Color {
        switch tone {
        case .info: return .secondary
        case .success: return .green
        case .warning: return .orange
        case .error: return .red
        }
    }
}

// MARK: - Access

struct AccessHelpView: View {
    @ObservedObject var model: AppModel

    var body: some View {
        ContentUnavailableView {
            Label("미리알림에 접근할 수 없어요", systemImage: "lock.shield")
        } description: {
            Text(message)
        } actions: {
            Button("시스템 설정 열기") {
                if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Reminders") {
                    NSWorkspace.shared.open(url)
                }
            }
            .buttonStyle(.borderedProminent)
            Button("다시 확인") { Task { await model.start() } }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private var message: String {
        switch model.access {
        case .restricted:
            return "이 Mac에서는 미리알림 접근이 제한되어 있어요."
        case .failed(let detail):
            return "권한을 확인하지 못했어요: \(detail)"
        default:
            return "시스템 설정 → 개인정보 보호 및 보안 → 미리알림에서 ‘미리알림 날짜’를 켠 뒤 ‘다시 확인’을 누르세요."
        }
    }
}
