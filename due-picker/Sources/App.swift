// App.swift — entry point, window and menu commands.

import AppKit
import SwiftUI

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}

@main
struct DuePickerApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var model = AppModel(backend: EventKitBackend())

    var body: some Scene {
        Window("미리알림 날짜", id: "main") {
            ContentView(model: model)
        }
        .defaultSize(width: 1220, height: 880)
        .commands { DueCommands(model: model) }
    }
}

struct DueCommands: Commands {
    @ObservedObject var model: AppModel

    var body: some Commands {
        CommandGroup(replacing: .undoRedo) {
            Button("실행 취소") { model.handleUndoCommand() }
                .keyboardShortcut("z", modifiers: .command)
            Button("실행 복귀") { NSApp.sendAction(Selector(("redo:")), to: nil, from: nil) }
                .keyboardShortcut("z", modifiers: [.command, .shift])
        }
        CommandGroup(replacing: .newItem) {
            Button("새 미리알림") { model.focusRequest = .quickAddTitle }
                .keyboardShortcut("n", modifiers: .command)
        }
        CommandMenu("날짜") {
            Button("말로 입력") { model.focusRequest = .dateInput }
                .keyboardShortcut("l", modifiers: .command)
            Divider()
            Group {
                Button("오늘") { model.apply(.today) }
                    .keyboardShortcut("1", modifiers: .command)
                Button("내일") { model.apply(.tomorrow) }
                    .keyboardShortcut("2", modifiers: .command)
                Button("모레") { model.apply(.dayAfterTomorrow) }
                    .keyboardShortcut("3", modifiers: .command)
                Button("이번 주말") { model.apply(.thisWeekend) }
                    .keyboardShortcut("4", modifiers: .command)
                Button("다음 주 월요일") { model.apply(.nextMonday) }
                    .keyboardShortcut("5", modifiers: .command)
            }
            .disabled(!model.hasSelection)
            Divider()
            Group {
                Button("하루 미루기") { model.apply(.postponeDay) }
                    .keyboardShortcut("]", modifiers: .command)
                Button("일주일 미루기") { model.apply(.postponeWeek) }
                    .keyboardShortcut("]", modifiers: [.command, .shift])
                Button("하루 당기기") { model.apply(.advanceDay) }
                    .keyboardShortcut("[", modifiers: .command)
                Divider()
                Button("종일로 바꾸기") { model.pick(time: nil) }
                Button("날짜 없음") { model.apply(DuePreset.clear) }
                    .keyboardShortcut("0", modifiers: .command)
            }
            .disabled(!model.hasSelection)
            Divider()
            Button("새로 고침") { Task { await model.reload() } }
                .keyboardShortcut("r", modifiers: .command)
        }
    }
}
