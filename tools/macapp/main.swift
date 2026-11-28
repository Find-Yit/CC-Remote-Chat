// CC Remote Chat 原生 macOS 壳（docs/SPEC_V2_TUI.md §14）。
//
// - AppKit + WKWebView 显示 http://127.0.0.1:<端口>/；程序坞、Cmd+Tab、最小化、窗口菜单、关于面板都是「CC Remote Chat」。
// - 后端 python3 app.py 是**独立的后台进程**（posix_spawn + POSIX_SPAWN_SETSID，stdin=/dev/null，输出追加到
//   <数据目录>/logs/launcher.log，环境变量白名单）：关窗口、退出本应用都不停后端；远端/本机的 claude 本来就不受影响。
// - 登录链接经数据目录交接：先删旧的 window_login_url → 建 window_request（0600）→ 运行 app.py（没有实例就启动；
//   已有实例它会发 SIGUSR1）→ 最多等 25 秒读到 window_login_url → 删掉 → 加载。
// - 重连：用户打开/激活应用时看 app.pid —— 后端不在 → 重新连接（会启动后端）；pid 变了 → 重新取登录链接；
//   页面显示"会话已失效" → 重新取登录链接；否则只把窗口提到前面。另外每 3 秒查一次 pid：变成新的存活 pid 自动重连；
//   后端死了**不**自动拉起（可能是用户主动 --stop）。
// - 不读写 ~/.claude*，不碰 Claude Code 的任何配置；只读写数据目录里的 window_request / window_login_url / launcher.log。
// - 内置模式（Info.plist CCRBundled=true，tools/make_dmg.sh 生成的 dmg 版，SPEC §14.10）：Python、app 代码、tmux 都在
//   .app 的 Resources 里，后端 = <Resources>/python/bin/python3 -I -B <Resources>/app/app.py --bundled --data-dir <目录>；
//   从 dmg / 隔离位置（AppTranslocation）/ 只读卷直接运行时不启动后端，只提示先拖到「应用程序」。没有 CCRBundled 时一切照旧。
// - 系统通知（SPEC §14.12，默认关闭）：页面经页面世界的 ccrNotify handler 请求发通知；开关存 UserDefaults CCRSysNotify，
//   只有用户在设置里第一次打开开关时才请求通知授权（启动时绝不请求）；点通知 → 激活窗口并打开对应会话。
//
// 编译（tools/make_app.sh 会做）：xcrun swiftc -O -swift-version 5 -target arm64-apple-macos12.0 \
//   -framework Cocoa -framework WebKit -o "CC Remote Chat" tools/macapp/main.swift
// 自测版另加 -D CCR_SELFTEST（命令文件驱动的自测钩子，正式版不编译进去）。

import Cocoa
import Darwin
import UserNotifications
import WebKit

let kAppName = "CC Remote Chat"
let kZoomKey = "CCRPageZoom"
let kZoomSteps: [CGFloat] = [0.5, 0.67, 0.75, 0.8, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0]
let kConnectTimeout: TimeInterval = 25
let kPidCheckInterval: TimeInterval = 3
let kBackendPath = "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin"
// 环境变量白名单：绝不能把调用者的 CLAUDECODE、CLAUDE_CODE_*、ANTHROPIC_* 等带进后端（之前出过问题）。
// __CF_USER_TEXT_ENCODING 与终端.app 新开窗口一致（不是凭据），保留以免 CoreFoundation 文本编码回退。
let kEnvWhitelist = ["HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "SSH_AUTH_SOCK", "LANG", "__CF_USER_TEXT_ENCODING"]

// MARK: - 配置（Info.plist，由 tools/make_app.sh 写入）

struct LaunchConfig {
    let bundled: Bool             // CCRBundled：内置模式（dmg 版，Python/app/tmux 都在 .app 里）
    let python: String            // CCRPython：python3 的绝对路径（内置模式 = <Resources>/python/bin/python3）
    let appPy: String             // CCRAppPy：app.py 的绝对路径
    let dataDir: URL              // CCRDataDir（仅自测版）或 ~/.cc-remote-chat（与 app.py 默认一致）
    let customDataDir: Bool
    let extraArgs: [String]       // CCRArgs（仅自测版，如 --fake --port 18xxx）
    let sandboxProfile: String?   // CCRSandboxProfile（仅自测版）：后端用 /usr/bin/sandbox-exec -f <profile> 包一层
    let downloadDir: URL          // CCRDownloadDir（仅自测版）或 ~/Downloads

    static func load() -> LaunchConfig {
        let info = Bundle.main.infoDictionary ?? [:]
        let home = URL(fileURLWithPath: NSHomeDirectory(), isDirectory: true)
        func str(_ k: String) -> String? {
            guard let v = info[k] as? String, !v.isEmpty else { return nil }
            return v
        }
        let dd = str("CCRDataDir")
        let dl = str("CCRDownloadDir")
        let bundled = (info["CCRBundled"] as? Bool) ?? (str("CCRBundled") == "true")
        let res = Bundle.main.resourceURL ?? Bundle.main.bundleURL.appendingPathComponent("Contents/Resources")
        // 内置模式：数据目录默认 ~/.cc-remote-chat-app（不和自己装的 ~/.cc-remote-chat 混在一起），且总是用 --data-dir 传给后端
        let defaultDir = bundled ? ".cc-remote-chat-app" : ".cc-remote-chat"
        return LaunchConfig(
            bundled: bundled,
            python: bundled ? res.appendingPathComponent("python/bin/python3").path : (str("CCRPython") ?? "/usr/bin/python3"),
            appPy: bundled ? res.appendingPathComponent("app/app.py").path : (str("CCRAppPy") ?? ""),
            dataDir: dd.map { URL(fileURLWithPath: ($0 as NSString).expandingTildeInPath, isDirectory: true) }
                ?? home.appendingPathComponent(defaultDir, isDirectory: true),
            customDataDir: dd != nil || bundled,
            extraArgs: (info["CCRArgs"] as? [String]) ?? [],
            sandboxProfile: str("CCRSandboxProfile"),
            downloadDir: dl.map { URL(fileURLWithPath: ($0 as NSString).expandingTildeInPath, isDirectory: true) }
                ?? home.appendingPathComponent("Downloads", isDirectory: true))
    }

    var logsDir: URL { dataDir.appendingPathComponent("logs", isDirectory: true) }
    var logFile: URL { logsDir.appendingPathComponent("launcher.log") }
    var requestFile: URL { dataDir.appendingPathComponent("window_request") }
    var loginURLFile: URL { dataDir.appendingPathComponent("window_login_url") }
    var pidFile: URL { dataDir.appendingPathComponent("app.pid") }

    var argv: [String] {
        var a: [String] = []
        if let sb = sandboxProfile { a += ["/usr/bin/sandbox-exec", "-f", sb] }
        if bundled {
            // -I：不看 PYTHON* 环境变量与用户 site-packages；-B：不往 .app 里写 __pycache__（会破坏签名）
            return a + [python, "-I", "-B", appPy, "--bundled", "--data-dir", dataDir.path] + extraArgs
        }
        a += [python, appPy]
        if customDataDir { a += ["--data-dir", dataDir.path] }
        return a + extraArgs
    }

    /// 内置模式下不适合运行的位置（返回原因；nil = 可以运行）：还在 dmg 里（/Volumes/…）、被 Gatekeeper 隔离运行
    /// （…/AppTranslocation/…，每次路径都不同，后端路径对不上）、或所在卷只读。非内置模式永远返回 nil。
    var placementProblem: String? {
        guard bundled else { return nil }
        let p = Bundle.main.bundlePath
        if p.contains("/AppTranslocation/") { return "隔离位置（AppTranslocation）" }
        if p.hasPrefix("/Volumes/") { return "磁盘映像或外接卷（/Volumes）" }
        var fs = statfs()
        if statfs(p, &fs) == 0 && (fs.f_flags & UInt32(MNT_RDONLY)) != 0 { return "只读卷" }
        return nil
    }
}

// MARK: - 日志（追加到 launcher.log，与后端输出同一个文件）

enum NativeLog {
    static let queue = DispatchQueue(label: "ccr.native.log")
    static var path: String = ""

    static func write(_ msg: String) {
        let p = path
        guard !p.isEmpty else { return }
        let df = DateFormatter()
        df.locale = Locale(identifier: "en_US_POSIX")
        df.dateFormat = "yyyy-MM-dd HH:mm:ss,SSS"
        let line = "\(df.string(from: Date())) [原生应用] \(msg)\n"
        queue.async {
            let fd = open(p, O_WRONLY | O_CREAT | O_APPEND | O_NOFOLLOW | O_CLOEXEC, 0o600)
            guard fd >= 0 else { return }
            _ = line.withCString { Darwin.write(fd, $0, strlen($0)) }
            close(fd)
        }
    }
}

// MARK: - 后端进程（不依赖 AppKit，在后台线程运行）

final class ChildWatch {
    private let lock = NSLock()
    private var _code: Int32? = nil
    var exitCode: Int32? { lock.lock(); defer { lock.unlock() }; return _code }
    func set(_ c: Int32) { lock.lock(); _code = c; lock.unlock() }
}

enum ConnectOutcome {
    case success(URL, pid_t?)
    case failure(String)
}

enum Backend {
    /// app.pid（{"pid": N}）里的进程是否还活着且像是 app.py（防 pid 复用）；返回该 pid 或 nil
    static func livePid(_ cfg: LaunchConfig) -> pid_t? {
        guard let data = try? Data(contentsOf: cfg.pidFile, options: []),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let n = (obj["pid"] as? NSNumber)?.int32Value, n > 1 else { return nil }
        if kill(n, 0) != 0 && errno != EPERM { return nil }
        if let args = processArgs(n) {
            // 内置模式：必须是**本 .app 里的** app.py（完整路径）；app 被挪过位置时旧后端不算（见 stalePid）
            if cfg.bundled { return args.contains(cfg.appPy) ? n : nil }
            return args.contains(where: { ($0 as NSString).lastPathComponent == "app.py" }) ? n : nil
        }
        return n
    }

    /// 内置模式：app.pid 里是活着的 app.py，但不是本 .app 里那份（app 被挪过位置 / 换了新版本到别处）→ 返回其 pid
    static func stalePid(_ cfg: LaunchConfig) -> pid_t? {
        guard cfg.bundled,
              let data = try? Data(contentsOf: cfg.pidFile, options: []),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let n = (obj["pid"] as? NSNumber)?.int32Value, n > 1, kill(n, 0) == 0,
              let args = processArgs(n),
              args.contains(where: { ($0 as NSString).lastPathComponent == "app.py" }),
              !args.contains(cfg.appPy) else { return nil }
        return n
    }

    /// 先 SIGTERM 旧位置的后端并等它退出（释放实例锁；tmux 里的 claude 不受影响），成功返回 true
    static func stopStale(_ cfg: LaunchConfig) -> Bool {
        guard let old = stalePid(cfg) else { return true }
        NativeLog.write("app 位置变了：先停止旧位置的后端（pid \(old)）")
        kill(old, SIGTERM)
        let deadline = Date().addingTimeInterval(15)
        while Date() < deadline {
            if kill(old, 0) != 0 && errno == ESRCH { return true }
            usleep(200_000)
        }
        return false
    }

    /// KERN_PROCARGS2：读取进程的 argv（同一用户的进程不需要特权）
    static func processArgs(_ pid: pid_t) -> [String]? {
        var argmax: Int32 = 0
        var sz = MemoryLayout<Int32>.size
        var mib0: [Int32] = [CTL_KERN, KERN_ARGMAX]
        guard sysctl(&mib0, 2, &argmax, &sz, nil, 0) == 0, argmax > 0 else { return nil }
        var buf = [UInt8](repeating: 0, count: Int(argmax))
        var size = buf.count
        var mib: [Int32] = [CTL_KERN, KERN_PROCARGS2, pid]
        guard sysctl(&mib, 3, &buf, &size, nil, 0) == 0, size > 4 else { return nil }
        var argc: Int32 = 0
        withUnsafeMutableBytes(of: &argc) { $0.copyBytes(from: buf[0..<4]) }
        var i = 4
        while i < size && buf[i] != 0 { i += 1 }   // 可执行文件路径
        while i < size && buf[i] == 0 { i += 1 }
        var out: [String] = []
        while out.count < Int(argc) && i < size {
            let s = i
            while i < size && buf[i] != 0 { i += 1 }
            out.append(String(decoding: buf[s..<i], as: UTF8.self))
            i += 1
        }
        return out
    }

    static func environment() -> [String] {
        let src = ProcessInfo.processInfo.environment
        var env: [String: String] = [:]
        for k in kEnvWhitelist { if let v = src[k], !v.isEmpty { env[k] = v } }
        if env["HOME"] == nil { env["HOME"] = NSHomeDirectory() }
        if env["USER"] == nil { env["USER"] = NSUserName() }
        if env["LOGNAME"] == nil { env["LOGNAME"] = env["USER"] }
        if env["LANG"] == nil { env["LANG"] = "zh_CN.UTF-8" }
        env["PATH"] = kBackendPath
        return env.map { "\($0.key)=\($0.value)" }.sorted()
    }

    static func ensureDir(_ url: URL) throws {
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true,
                                                attributes: [.posixPermissions: 0o700])
        chmod(url.path, 0o700)
    }

    /// 后台进程：新会话（与本应用脱离）、stdin=/dev/null、stdout/stderr 追加到 launcher.log、只继承这三个 fd、
    /// 信号处置与屏蔽字复位、cwd=HOME、环境变量白名单。
    static func spawn(_ cfg: LaunchConfig) throws -> pid_t {
        let argv = cfg.argv
        var fa: posix_spawn_file_actions_t? = nil
        posix_spawn_file_actions_init(&fa)
        defer { posix_spawn_file_actions_destroy(&fa) }
        posix_spawn_file_actions_addopen(&fa, 0, "/dev/null", O_RDONLY, 0)
        posix_spawn_file_actions_addopen(&fa, 1, cfg.logFile.path, O_WRONLY | O_CREAT | O_APPEND, 0o600)
        posix_spawn_file_actions_adddup2(&fa, 1, 2)
        posix_spawn_file_actions_addchdir_np(&fa, NSHomeDirectory())
        var attr: posix_spawnattr_t? = nil
        posix_spawnattr_init(&attr)
        defer { posix_spawnattr_destroy(&attr) }
        posix_spawnattr_setflags(&attr, Int16(POSIX_SPAWN_SETSID | POSIX_SPAWN_CLOEXEC_DEFAULT |
                                               POSIX_SPAWN_SETSIGDEF | POSIX_SPAWN_SETSIGMASK))
        var mask = sigset_t()
        sigemptyset(&mask)
        posix_spawnattr_setsigmask(&attr, &mask)
        var defs = sigset_t()
        sigfillset(&defs)
        sigdelset(&defs, SIGKILL)
        sigdelset(&defs, SIGSTOP)
        posix_spawnattr_setsigdefault(&attr, &defs)
        let cargs: [UnsafeMutablePointer<CChar>?] = argv.map { strdup($0) } + [nil]
        let cenv: [UnsafeMutablePointer<CChar>?] = environment().map { strdup($0) } + [nil]
        defer { cargs.forEach { free($0) }; cenv.forEach { free($0) } }
        var pid: pid_t = 0
        let rc = posix_spawn(&pid, argv[0], &fa, &attr, cargs, cenv)
        if rc != 0 {
            throw NSError(domain: NSPOSIXErrorDomain, code: Int(rc),
                          userInfo: [NSLocalizedDescriptionKey: String(cString: strerror(rc))])
        }
        return pid
    }

    /// 读 window_login_url：必须是本人的普通文件（不跟随软链接）、内容是本机 127.0.0.1 的登录链接
    static func readLoginURL(_ cfg: LaunchConfig) -> (URL?, Bool) {
        let fd = open(cfg.loginURLFile.path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC)
        if fd < 0 { return (nil, false) }
        defer { close(fd) }
        var st = stat()
        guard fstat(fd, &st) == 0, (st.st_mode & S_IFMT) == S_IFREG, st.st_uid == getuid(),
              st.st_size > 0, st.st_size < 4096 else { return (nil, true) }
        var buf = [UInt8](repeating: 0, count: Int(st.st_size))
        let n = read(fd, &buf, buf.count)
        guard n > 0 else { return (nil, true) }
        let s = String(decoding: buf[0..<n], as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
        guard let u = URL(string: s), u.scheme == "http", u.host == "127.0.0.1", let port = u.port, port > 0,
              u.path == "/" || u.path.isEmpty, (u.fragment ?? "").hasPrefix("code=") else { return (nil, true) }
        return (u, true)
    }

    /// 完整的"连接"：阻塞调用（后台线程）
    static func connect(_ cfg: LaunchConfig) -> ConnectOutcome {
        let fm = FileManager.default
        guard fm.isExecutableFile(atPath: cfg.python) else {
            if cfg.bundled { return .failure("app 不完整：找不到内置的 Python（\(cfg.python)）。请重新从 dmg 把 CC Remote Chat 拖到「应用程序」") }
            return .failure("找不到 Python：\(cfg.python)（请重新运行 tools/make_app.sh，或用 PYTHON=… 指定）")
        }
        guard !cfg.appPy.isEmpty, fm.fileExists(atPath: cfg.appPy) else {
            if cfg.bundled { return .failure("app 不完整：找不到 \(cfg.appPy)。请重新从 dmg 把 CC Remote Chat 拖到「应用程序」") }
            return .failure("找不到 app.py：\(cfg.appPy.isEmpty ? "（Info.plist 没有 CCRAppPy）" : cfg.appPy)（项目目录移动后需要重新运行 tools/make_app.sh）")
        }
        if cfg.bundled && !stopStale(cfg) {
            return .failure("旧位置的后端（app 挪动前启动的）15 秒内没有退出，请稍后点「重试」")
        }
        do {
            try ensureDir(cfg.dataDir)
            try ensureDir(cfg.logsDir)
        } catch {
            return .failure("无法创建数据目录 \(cfg.dataDir.path)：\(error.localizedDescription)")
        }
        // 1) 清掉旧的登录链接 2) 写请求文件（先删再 O_EXCL 新建：不会写到软链接指向的地方）
        unlink(cfg.loginURLFile.path)
        unlink(cfg.requestFile.path)
        let fd = open(cfg.requestFile.path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0o600)
        if fd < 0 {
            return .failure("无法创建 \(cfg.requestFile.path)：\(String(cString: strerror(errno)))")
        }
        let note = "\(getpid())\n"
        _ = note.withCString { Darwin.write(fd, $0, strlen($0)) }
        close(fd)
        // 3) 运行 app.py（没有实例 → 成为常驻后端；已有实例 → 发 SIGUSR1 后退出）
        NativeLog.write("运行后端：\(cfg.argv.joined(separator: " "))")
        let pid: pid_t
        do {
            pid = try spawn(cfg)
        } catch {
            unlink(cfg.requestFile.path)
            return .failure("无法启动后端（\(cfg.argv.joined(separator: " "))）：\(error.localizedDescription)")
        }
        let watch = ChildWatch()
        Thread.detachNewThread {   // 回收子进程，避免僵尸（常驻后端会一直阻塞在这里，属正常）
            var status: Int32 = 0
            while waitpid(pid, &status, 0) == -1 && errno == EINTR {}
            let sig = status & 0x7f
            watch.set(sig == 0 ? (status >> 8) & 0xff : 128 + sig)
        }
        // 4) 轮询登录链接
        let deadline = Date().addingTimeInterval(kConnectTimeout)
        while Date() < deadline {
            let (url, present) = readLoginURL(cfg)
            if present {
                unlink(cfg.loginURLFile.path)
                guard let url = url else {
                    unlink(cfg.requestFile.path)
                    return .failure("window_login_url 内容不合法")
                }
                return .success(url, livePid(cfg))
            }
            if let code = watch.exitCode, code != 0 {
                usleep(200_000)
                if case (let u?, true) = readLoginURL(cfg) {
                    unlink(cfg.loginURLFile.path)
                    return .success(u, livePid(cfg))
                }
                unlink(cfg.requestFile.path)
                return .failure("后端启动失败（app.py 退出码 \(code)）")
            }
            usleep(100_000)
        }
        unlink(cfg.requestFile.path)   // 不让残留请求劫持之后在终端里手动运行的实例
        return .failure("\(Int(kConnectTimeout)) 秒内没有拿到登录链接")
    }
}

// MARK: - 本地页面（连接中 / 错误）

func htmlEscape(_ s: String) -> String {
    var o = ""
    for c in s {
        switch c {
        case "&": o += "&amp;"
        case "<": o += "&lt;"
        case ">": o += "&gt;"
        case "\"": o += "&quot;"
        case "'": o += "&#39;"
        default: o.append(c)
        }
    }
    return o
}

let kPageCSS = """
:root{color-scheme:light dark;--bg:#fafafa;--card:#fff;--text:#1f2328;--muted:#636973;--line:#e4e6ea;--accent:#d97757}
@media (prefers-color-scheme:dark){:root{--bg:#1b1c1f;--card:#24262a;--text:#e8e9eb;--muted:#a0a5ad;--line:#33363b}}
html,body{margin:0;height:100%;background:var(--bg);color:var(--text);font:14px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif}
.wrap{display:flex;align-items:center;justify-content:center;height:100%;padding:24px;box-sizing:border-box}
.card{max-width:620px;width:100%;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:24px 28px}
h1{font-size:18px;margin:0 0 8px}.muted{color:var(--muted)}code{font:12px ui-monospace,Menlo,monospace;word-break:break-all}
.reason{margin:12px 0;padding:10px 12px;border-radius:8px;background:rgba(217,119,87,.10);border:1px solid rgba(217,119,87,.35)}
.btns{margin-top:16px;display:flex;gap:10px;flex-wrap:wrap}
a.btn{display:inline-block;padding:6px 16px;border-radius:8px;border:1px solid var(--line);color:var(--text);text-decoration:none}
a.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}
ul{padding-left:20px;margin:8px 0}.spin{width:22px;height:22px;border-radius:50%;border:3px solid var(--line);border-top-color:var(--accent);animation:r 0.9s linear infinite;margin-bottom:12px}
@keyframes r{to{transform:rotate(360deg)}}
"""

func loadingPageHTML() -> String {
    return """
    <!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>\(kAppName)</title><style>\(kPageCSS)</style></head>
    <body><div class="wrap"><div class="card"><div class="spin"></div><h1>正在连接后端…</h1>
    <div class="muted">正在启动或唤醒本机的 python3 app.py（只监听 127.0.0.1）。</div></div></div></body></html>
    """
}

/// 内置模式放错位置（还在 dmg 里 / 被隔离运行 / 只读卷）：不启动后端，只提示怎么做
func placementPageHTML(_ why: String) -> String {
    return """
    <!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>\(kAppName)</title><style>\(kPageCSS)</style></head>
    <body><div class="wrap"><div class="card"><h1>请先把 CC Remote Chat 拖到「应用程序」文件夹，再从那里打开</h1>
    <div class="reason">现在是从\(htmlEscape(why))里直接打开的，这样没法正常运行。</div>
    <ul class="muted">
    <li>退出本应用（⌘Q），把 dmg 窗口里的 CC Remote Chat 拖到旁边的「应用程序」文件夹；</li>
    <li>然后在「应用程序」里双击打开。第一次打开如果提示无法验证开发者，见 dmg 里的「先读我.txt」。</li>
    </ul></div></div></body></html>
    """
}

func errorPageHTML(reason: String, cfg: LaunchConfig) -> String {
    if cfg.bundled { return bundledErrorPageHTML(reason: reason, cfg: cfg) }
    let stop = "\"\(cfg.python)\" \"\(cfg.appPy)\"" + (cfg.customDataDir ? " --data-dir \"\(cfg.dataDir.path)\"" : "") + " --stop"
    return """
    <!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>\(kAppName)</title><style>\(kPageCSS)</style></head>
    <body><div class="wrap"><div class="card"><h1>无法连接到后端</h1>
    <div class="reason">\(htmlEscape(reason))</div>
    <div class="muted">后端日志：<code>\(htmlEscape(cfg.logFile.path))</code></div>
    <ul class="muted">
    <li>如果正在运行的后端是旧版本（不支持原生窗口，会改为打开 Chrome），请先在终端运行
    <code>\(htmlEscape(stop))</code>，再点「重试」。</li>
    <li>项目目录移动过：重新运行 <code>tools/make_app.sh</code>。</li>
    </ul>
    <div class="btns"><a class="btn primary" href="ccr-native://retry">重试</a>
    <a class="btn" href="ccr-native://reveal-log">在 Finder 中显示日志</a></div></div></div></body></html>
    """
}

/// 内置模式的错误页：不提 make_app.sh / 项目目录（dmg 版没有这些），给能照着做的说明
func bundledErrorPageHTML(reason: String, cfg: LaunchConfig) -> String {
    return """
    <!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>\(kAppName)</title><style>\(kPageCSS)</style></head>
    <body><div class="wrap"><div class="card"><h1>无法连接到后端</h1>
    <div class="reason">\(htmlEscape(reason))</div>
    <div class="muted">日志：<code>\(htmlEscape(cfg.logFile.path))</code></div>
    <ul class="muted">
    <li>先点「重试」；还不行就退出本应用（⌘Q）再重新打开。</li>
    <li>刚把 app 换了位置或换了新版本：退出后从「应用程序」里重新打开。</li>
    <li>如果一直失败，请把上面的日志文件发给给你 app 的人。</li>
    </ul>
    <div class="btns"><a class="btn primary" href="ccr-native://retry">重试</a>
    <a class="btn" href="ccr-native://reveal-log">在 Finder 中显示日志</a></div></div></div></body></html>
    """
}

// MARK: - 拖入文件（SPEC §7「图片」、§14.6）
// 终端里把文件拖进去 = 终端把文件路径作为一次粘贴送给 claude（claude 自己把图片路径变成 [Image #N]）。WKWebView 自己处理
// 拖放时网页只拿得到文件内容、拿不到路径，所以这里截下"带本机文件 URL 的拖动"（Finder 里的文件、截屏后右下角的浮动
// 缩略图），把路径以 `ccr-native-drag`（phase: enter / leave）/ `ccr-native-drop` 事件交给页面；页面按当前会话决定：
// 本机会话的图片 → 后端粘贴路径进 claude 输入框，其它文件 → 路径插进输入框，服务器会话 → 提示。转义（终端拖放的写法）
// 在页面 / 后端做（static/attach.js、ccr/attach.py，有单测）。只在显示本应用页面时截；没有文件 URL 的拖动（页面里拖文字、
// 从浏览器拖来的网页图片等）一律照常交给 WebKit。本类不读写剪贴板（拖动用的是单独的拖动粘贴板），也不读文件内容。
// BEGIN ccr-drop-script（tests/test_native_drop.py 抽出这段单独编译，用 JavaScriptCore 核对路径原样到达页面）
/// 交给页面的事件脚本：JSON 由 JSONSerialization 生成（路径里的引号、反斜杠、换行、</script> 都只是字符串内容），不拼接 HTML。
/// phase 只能是 enter / leave / drop。
func ccrDropEventScript(_ phase: String, _ paths: [String]) -> String? {
    guard ["enter", "leave", "drop"].contains(phase) else { return nil }
    let detail: [String: Any] = ["phase": phase, "paths": paths]
    guard let data = try? JSONSerialization.data(withJSONObject: detail),
          let json = String(data: data, encoding: .utf8) else { return nil }
    let name = phase == "drop" ? "ccr-native-drop" : "ccr-native-drag"
    return "window.dispatchEvent(new CustomEvent('\(name)', { detail: \(json) }));"
}
// END ccr-drop-script

final class CCRWebView: WKWebView {
    static let maxDropFiles = 20
    var acceptsFileDrops: () -> Bool = { false }
    private var fileDrag = false   // 当前这次拖动由本类处理（WebKit 完全不知道它）

    /// 拖动粘贴板上的本机文件 URL（最多 maxDropFiles 个）
    static func fileURLs(_ pb: NSPasteboard) -> [URL] {
        let opts: [NSPasteboard.ReadingOptionKey: Any] = [.urlReadingFileURLsOnly: true]
        let urls = (pb.readObjects(forClasses: [NSURL.self], options: opts) as? [URL]) ?? []
        return Array(urls.filter { $0.isFileURL }.prefix(maxDropFiles))
    }

    private func notify(_ phase: String, _ urls: [URL]) {
        guard let js = ccrDropEventScript(phase, urls.map { $0.path }) else { return }
        evaluateJavaScript(js, completionHandler: nil)
    }

    override func draggingEntered(_ sender: NSDraggingInfo) -> NSDragOperation {
        let urls = Self.fileURLs(sender.draggingPasteboard)
        if !urls.isEmpty && acceptsFileDrops() {
            fileDrag = true
            notify("enter", urls)
            return .copy
        }
        fileDrag = false
        return super.draggingEntered(sender)
    }

    override func draggingUpdated(_ sender: NSDraggingInfo) -> NSDragOperation {
        return fileDrag ? .copy : super.draggingUpdated(sender)
    }

    override func draggingExited(_ sender: NSDraggingInfo?) {
        guard fileDrag else { return super.draggingExited(sender) }
        fileDrag = false
        notify("leave", [])
    }

    override func prepareForDragOperation(_ sender: NSDraggingInfo) -> Bool {
        return fileDrag ? true : super.prepareForDragOperation(sender)
    }

    override func performDragOperation(_ sender: NSDraggingInfo) -> Bool {
        guard fileDrag else { return super.performDragOperation(sender) }
        let urls = Self.fileURLs(sender.draggingPasteboard)
        notify(urls.isEmpty ? "leave" : "drop", urls)
        NativeLog.write("拖入 \(urls.count) 个文件（路径已交给页面）")
        return !urls.isEmpty
    }

    override func concludeDragOperation(_ sender: NSDraggingInfo?) {
        guard fileDrag else { return super.concludeDragOperation(sender) }
        fileDrag = false
    }
}

// MARK: - 应用

@MainActor
final class AppController: NSObject, NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate,
    WKDownloadDelegate, WKScriptMessageHandler {

    enum PageState { case blank, loading, error, app }

    let cfg = LaunchConfig.load()
    var window: NSWindow!
    var webView: WKWebView!
    var titleObs: NSKeyValueObservation?
    var pidTimer: Timer?
    var pageState: PageState = .blank
    var connecting = false
    var knownPid: pid_t? = nil          // 上次连接（成功或失败）时看到的后端 pid
    var appPort: Int? = nil             // 当前页面的端口（http://127.0.0.1:<端口>）
    var authLost = false
    var autoReconnects: [Date] = []
    var hideAfterFullScreen = false
    var downloads: [ObjectIdentifier: URL] = [:]
    let sysNotifier = SysNotifier()     // 系统通知（页面世界的 ccrNotify handler，SPEC §14.12）
    #if CCR_SELFTEST
    var selfTest: SelfTest?
    #endif

    // MARK: 生命周期
    func applicationDidFinishLaunching(_ notification: Notification) {
        NativeLog.path = cfg.logFile.path
        try? Backend.ensureDir(cfg.dataDir)
        try? Backend.ensureDir(cfg.logsDir)
        UserDefaults.standard.register(defaults: ["NSFullScreenMenuItemEverywhere": false])
        sysNotifier.setUp(self)   // 只在开关打开过时设 delegate（点通知打开会话）；不请求授权
        buildMenu()
        buildWindow()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        NativeLog.write("启动（\(Bundle.main.bundleIdentifier ?? "?")，数据目录 \(cfg.dataDir.path)）")
        connect(reason: "启动")
        pidTimer = Timer.scheduledTimer(withTimeInterval: kPidCheckInterval, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.checkPid() }
        }
        #if CCR_SELFTEST
        selfTest = SelfTest(controller: self)
        #endif
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showWindow()
        ensureBackend(trigger: "重新打开")
        return false
    }

    func applicationDidBecomeActive(_ notification: Notification) {
        ensureBackend(trigger: "激活")
    }

    func applicationWillTerminate(_ notification: Notification) {
        NativeLog.write("退出（后端继续在后台运行）")
    }

    // MARK: 窗口
    func buildWindow() {
        let conf = WKWebViewConfiguration()
        conf.websiteDataStore = .default()
        conf.preferences.javaScriptCanOpenWindowsAutomatically = false
        if #available(macOS 12.3, *) { conf.preferences.isElementFullscreenEnabled = true }
        conf.applicationNameForUserAgent = "CCRemoteChat/1.0"
        let ucc = conf.userContentController
        // 只在隔离的 defaultClient 世界里注入：页面脚本看不到 ccrNative，也碰不到这段脚本
        let js = """
        (() => {
          const hd = window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.ccrNative;
          if (!hd || !document.body) return;
          let shown = false;
          const check = () => {
            const on = !!document.querySelector('body > .auth-lost');
            if (on !== shown) { shown = on; hd.postMessage({ type: on ? 'authLost' : 'authOk' }); }
          };
          new MutationObserver(check).observe(document.body, { childList: true });
          check();
        })();
        """
        ucc.addUserScript(WKUserScript(source: js, injectionTime: .atDocumentEnd, forMainFrameOnly: true,
                                       in: .defaultClient))
        ucc.add(self, contentWorld: .defaultClient, name: "ccrNative")
        // 系统通知：页面脚本要能调用，所以在页面世界；消息只认主框架、本应用 origin（SysNotifier 里校验）
        ucc.addScriptMessageHandler(sysNotifier, contentWorld: .page, name: "ccrNotify")

        let wv = CCRWebView(frame: NSRect(x: 0, y: 0, width: 1280, height: 820), configuration: conf)
        // 拖入文件：只在显示本应用页面时把路径交给页面（错误页 / 连接中页面照常交给 WebKit，file: 导航会被下面的策略拦掉）
        wv.acceptsFileDrops = { [weak self] in self?.pageState == .app }
        webView = wv
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.allowsBackForwardNavigationGestures = false
        webView.allowsMagnification = false
        webView.autoresizingMask = [.width, .height]
        if #available(macOS 13.3, *) { webView.isInspectable = true }
        let z = UserDefaults.standard.double(forKey: kZoomKey)
        webView.pageZoom = z > 0 ? CGFloat(z) : 1.0
        titleObs = webView.observe(\.title, options: [.new]) { [weak self] _, _ in
            DispatchQueue.main.async { self?.updateTitle() }
        }

        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1280, height: 820),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = kAppName
        window.isReleasedWhenClosed = false
        window.isRestorable = false
        window.tabbingMode = .disallowed
        window.collectionBehavior = [.fullScreenPrimary]
        window.minSize = NSSize(width: 640, height: 420)
        window.contentView = webView
        window.delegate = self
        window.center()
        window.setFrameAutosaveName("CCRMainWindow")
    }

    func updateTitle() {
        let t = (webView.title ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        window.title = t.isEmpty ? kAppName : t
        // 侧栏「新回复」：页面把未读数写在标题前缀 "(n) "（static/sessrow.js docTitle）→ 程序坞角标。
        // 关窗只是 orderOut，页面和 SSE 还活着，标题照常变化，所以关窗后角标也会更新
        let n = Self.unreadCount(t)
        let badge: String? = n > 0 ? "\(n)" : nil
        if NSApp.dockTile.badgeLabel != badge { NSApp.dockTile.badgeLabel = badge }
    }

    /// "(n) ..." → n（1–6 位数字，后面必须跟一个空格）；其它 → 0。与 static/sessrow.js 的 titleCount 同一规则
    static func unreadCount(_ title: String) -> Int {
        guard title.hasPrefix("("), let close = title.firstIndex(of: ")") else { return 0 }
        let digits = title[title.index(after: title.startIndex)..<close]
        let rest = title[title.index(after: close)...]
        guard (1...6).contains(digits.count), digits.allSatisfy({ $0.isASCII && $0.isNumber }),
              rest.hasPrefix(" "), let n = Int(digits) else { return 0 }
        return n
    }

    /// 窗口在前台：应用是活动的、窗口是主窗口、看得见（没关、没最小化、没被完全挡住）
    func isForeground() -> Bool {
        return NSApp.isActive && window.isKeyWindow && window.isVisible && !window.isMiniaturized
            && window.occlusionState.contains(.visible)
    }

    func showWindow() {
        if window.isMiniaturized { window.deminiaturize(nil) }
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        // 关闭 = 只隐藏（不销毁 WebView、不断开 SSE）；点程序坞图标重新显示
        if window.styleMask.contains(.fullScreen) {
            hideAfterFullScreen = true
            window.toggleFullScreen(nil)
        } else {
            window.orderOut(nil)
        }
        return false
    }

    func windowDidExitFullScreen(_ notification: Notification) {
        if hideAfterFullScreen {
            hideAfterFullScreen = false
            window.orderOut(nil)
        }
    }

    // MARK: 菜单
    func buildMenu() {
        let main = NSMenu()
        func item(_ title: String, _ action: Selector?, _ key: String = "",
                  _ mods: NSEvent.ModifierFlags = [.command], target: AnyObject? = nil) -> NSMenuItem {
            let it = NSMenuItem(title: title, action: action, keyEquivalent: key)
            it.keyEquivalentModifierMask = mods
            it.target = target
            return it
        }
        func submenu(_ title: String, _ items: [NSMenuItem]) -> NSMenu {
            let m = NSMenu(title: title)
            items.forEach { m.addItem($0) }
            let holder = NSMenuItem(title: title, action: nil, keyEquivalent: "")
            holder.submenu = m
            main.addItem(holder)
            return m
        }
        _ = submenu(kAppName, [
            item("关于 \(kAppName)", #selector(showAbout(_:)), "", [], target: self),
            .separator(),
            item("隐藏 \(kAppName)", #selector(NSApplication.hide(_:)), "h"),
            item("隐藏其他", #selector(NSApplication.hideOtherApplications(_:)), "h", [.command, .option]),
            item("全部显示", #selector(NSApplication.unhideAllApplications(_:))),
            .separator(),
            item("退出 \(kAppName)", #selector(NSApplication.terminate(_:)), "q"),
        ])
        _ = submenu("编辑", [
            item("撤销", Selector(("undo:")), "z"),
            item("重做", Selector(("redo:")), "z", [.command, .shift]),
            .separator(),
            item("剪切", #selector(NSText.cut(_:)), "x"),
            item("拷贝", #selector(NSText.copy(_:)), "c"),
            item("粘贴", #selector(NSText.paste(_:)), "v"),
            item("全选", #selector(NSText.selectAll(_:)), "a"),
            .separator(),
            // 会话内查找（static/finder.js）：按键先交给网页（网页处理了就不会走到菜单）；从菜单栏点 = 派发 ccr-find 事件
            item("查找…", #selector(findInPage(_:)), "f", target: self),
            item("查找下一个", #selector(findNextInPage(_:)), "g", target: self),
            item("查找上一个", #selector(findPrevInPage(_:)), "g", [.command, .shift], target: self),
        ])
        _ = submenu("显示", [
            item("重新载入", #selector(reloadPage(_:)), "r", target: self),
            .separator(),
            item("实际大小", #selector(zoomReset(_:)), "0", target: self),
            item("放大", #selector(zoomIn(_:)), "=", target: self),
            item("缩小", #selector(zoomOut(_:)), "-", target: self),
            .separator(),
            item("进入全屏", #selector(NSWindow.toggleFullScreen(_:)), "f", [.command, .control]),
        ])
        let win = submenu("窗口", [
            item("最小化", #selector(NSWindow.performMiniaturize(_:)), "m"),
            item("缩放", #selector(NSWindow.performZoom(_:))),
            item("关闭", #selector(NSWindow.performClose(_:)), "w"),
            .separator(),
            item("前置全部窗口", #selector(NSApplication.arrangeInFront(_:))),
        ])
        NSApp.mainMenu = main
        NSApp.windowsMenu = win
    }

    @objc func showAbout(_ sender: Any?) {
        let ver = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? ""
        let credits = NSAttributedString(
            string: "本地聊天界面，驱动官方交互式 Claude Code CLI。\n后端 python3 app.py 是独立的后台进程：关窗口、退出本应用都不会停止它。",
            attributes: [.font: NSFont.systemFont(ofSize: 11), .foregroundColor: NSColor.secondaryLabelColor])
        NSApp.orderFrontStandardAboutPanel(options: [.applicationName: kAppName, .applicationVersion: ver,
                                                     .version: "", .credits: credits])
        NSApp.activate(ignoringOtherApps: true)
    }

    // 查找（⌘F / ⌘G / ⇧⌘G）：WKWebView 把带 ⌘ 的按键先交给网页，网页的查找栏 preventDefault 后菜单不会再收到；
    // 从菜单栏点（或网页没处理）时把动作以 ccr-find 事件交给页面（action 只是这三个常量，不拼接外部输入）
    @objc func findInPage(_ sender: Any?) { sendFind("open") }
    @objc func findNextInPage(_ sender: Any?) { sendFind("next") }
    @objc func findPrevInPage(_ sender: Any?) { sendFind("prev") }
    func sendFind(_ action: String) {
        guard pageState == .app, ["open", "next", "prev"].contains(action) else { return }
        webView.evaluateJavaScript("window.dispatchEvent(new CustomEvent('ccr-find', { detail: { action: '\(action)' } }));", completionHandler: nil)
    }

    @objc func reloadPage(_ sender: Any?) {
        switch pageState {
        case .app: webView.reload()
        case .error, .blank: connect(reason: "重新载入")
        case .loading: break
        }
    }

    func setZoom(_ z: CGFloat) {
        let v = min(max(z, kZoomSteps.first!), kZoomSteps.last!)
        webView.pageZoom = v
        UserDefaults.standard.set(Double(v), forKey: kZoomKey)
    }
    @objc func zoomReset(_ sender: Any?) { setZoom(1.0) }
    @objc func zoomIn(_ sender: Any?) {
        let cur = webView.pageZoom
        setZoom(kZoomSteps.first(where: { $0 > cur + 0.001 }) ?? kZoomSteps.last!)
    }
    @objc func zoomOut(_ sender: Any?) {
        let cur = webView.pageZoom
        setZoom(kZoomSteps.last(where: { $0 < cur - 0.001 }) ?? kZoomSteps.first!)
    }

    // MARK: 连接 / 重连
    func connect(reason: String) {
        if connecting { return }
        if let why = cfg.placementProblem {   // 内置模式放错位置：不启动后端
            NativeLog.write("不在合适的位置运行（\(why)：\(Bundle.main.bundlePath)），不启动后端")
            showLocal(.error, html: placementPageHTML(why))
            return
        }
        connecting = true
        NativeLog.write("连接（\(reason)）")
        if pageState != .app { showLocal(.loading, html: loadingPageHTML()) }
        let cfg = self.cfg
        DispatchQueue.global(qos: .userInitiated).async {
            let r = Backend.connect(cfg)
            DispatchQueue.main.async { self.connectDone(r) }
        }
    }

    func connectDone(_ r: ConnectOutcome) {
        connecting = false
        switch r {
        case .success(let url, let pid):
            knownPid = pid ?? Backend.livePid(cfg)
            appPort = url.port
            authLost = false
            pageState = .app
            NativeLog.write("已拿到登录链接（端口 \(url.port ?? 0)，后端 pid \(knownPid.map { String($0) } ?? "?")）")
            webView.load(URLRequest(url: url))
        case .failure(let msg):
            knownPid = Backend.livePid(cfg)
            NativeLog.write("连接失败：\(msg)")
            showLocal(.error, html: errorPageHTML(reason: msg, cfg: cfg))
        }
    }

    func showLocal(_ st: PageState, html: String) {
        pageState = st
        webView.loadHTMLString(html, baseURL: nil)
    }

    /// 用户打开/激活应用时：后端不在 → 启动；pid 变了 / 页面"会话已失效" → 重新取登录链接；否则什么都不做
    func ensureBackend(trigger: String) {
        if connecting { return }
        let live = Backend.livePid(cfg)
        if live == nil {
            connect(reason: "\(trigger)：后端未运行")
        } else if live != knownPid {
            connect(reason: "\(trigger)：后端 pid \(knownPid.map { String($0) } ?? "无") → \(live!)")
        } else if pageState == .app && authLost {
            connect(reason: "\(trigger)：页面会话已失效")
        }
    }

    /// 每 3 秒：pid 变成新的存活进程（后端被重启过，例如部署）→ 自动重连；后端死了不自动拉起
    func checkPid() {
        if connecting { return }
        guard let live = Backend.livePid(cfg), live != knownPid else { return }
        connect(reason: "后端 pid \(knownPid.map { String($0) } ?? "无") → \(live)")
    }

    // MARK: WKScriptMessageHandler（隔离世界里的"会话已失效"探测）
    func userContentController(_ ucc: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.frameInfo.isMainFrame, isAppURL(message.frameInfo.request.url) || isAppOrigin(message.frameInfo.securityOrigin),
              let body = message.body as? [String: Any], let type = body["type"] as? String else { return }
        if type == "authOk" { authLost = false; return }
        guard type == "authLost" else { return }
        authLost = true
        // 后端还活着才自动重取登录链接（例如 WebContent 崩溃后 sessionStorage 丢了）；5 分钟内最多 3 次
        let now = Date()
        autoReconnects = autoReconnects.filter { now.timeIntervalSince($0) < 300 }
        if !connecting, Backend.livePid(cfg) != nil, autoReconnects.count < 3 {
            autoReconnects.append(now)
            connect(reason: "页面会话已失效")
        }
    }

    // MARK: 导航策略
    func isAppOrigin(_ o: WKSecurityOrigin) -> Bool {
        return o.protocol == "http" && o.host == "127.0.0.1" && appPort != nil && o.port == appPort!
    }

    func isAppURL(_ u: URL?) -> Bool {
        guard let u = u, let p = appPort else { return false }
        return u.scheme == "http" && u.host == "127.0.0.1" && u.port == p
    }

    func openExternal(_ u: URL) {
        guard let s = u.scheme?.lowercased(), ["http", "https", "mailto"].contains(s) else {
            NativeLog.write("忽略外部链接（不支持的协议）：\(u.scheme ?? "?")")
            return
        }
        #if CCR_SELFTEST
        if selfTest?.recordExternal(u) == true { return }
        #endif
        NSWorkspace.shared.open(u)
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else { return decisionHandler(.cancel) }
        let scheme = (url.scheme ?? "").lowercased()
        let main = navigationAction.targetFrame?.isMainFrame
        if scheme == "ccr-native" {   // 本地错误页上的按钮
            decisionHandler(.cancel)
            if main == true && pageState != .app { handleNativeAction(url.host ?? "") }
            return
        }
        if navigationAction.shouldPerformDownload { return decisionHandler(.download) }
        if main == nil {   // target=_blank：交给默认浏览器
            decisionHandler(.cancel)
            openExternal(url)
            return
        }
        if main == false {   // 子框架（预览 iframe）：照常加载网页类地址
            return decisionHandler(["http", "https", "about", "data", "blob"].contains(scheme) ? .allow : .cancel)
        }
        if scheme == "about" { return decisionHandler(.allow) }
        if isAppURL(url) && !url.path.hasPrefix("/preview/") { return decisionHandler(.allow) }
        // 主框架里跳到非本应用 origin（或本应用的 /preview/ 预览地址）→ 默认浏览器
        decisionHandler(.cancel)
        openExternal(url)
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationResponse: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        if !navigationResponse.canShowMIMEType { return decisionHandler(.download) }
        if let r = navigationResponse.response as? HTTPURLResponse,
           let cd = r.value(forHTTPHeaderField: "Content-Disposition"), cd.lowercased().hasPrefix("attachment") {
            return decisionHandler(.download)
        }
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) {
        download.delegate = self
    }

    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) {
        download.delegate = self
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        NativeLog.write("网页进程崩溃，自动重新载入")
        switch pageState {
        case .app: webView.reload()   // 若 token 丢了，页面会显示"会话已失效" → 自动重新取登录链接
        case .loading: webView.loadHTMLString(loadingPageHTML(), baseURL: nil)
        case .error, .blank: connect(reason: "网页进程崩溃")
        }
    }

    func handleNativeAction(_ action: String) {
        switch action {
        case "retry": connect(reason: "用户点了重试")
        case "reveal-log":
            if FileManager.default.fileExists(atPath: cfg.logFile.path) {
                NSWorkspace.shared.activateFileViewerSelecting([cfg.logFile])
            } else {
                NSWorkspace.shared.activateFileViewerSelecting([cfg.logsDir])
            }
        default: break
        }
    }

    // MARK: WKUIDelegate
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let u = navigationAction.request.url { openExternal(u) }   // window.open / target=_blank
        return nil
    }

    func presentAlert(_ alert: NSAlert, _ done: @escaping (NSApplication.ModalResponse) -> Void) {
        if window.isVisible && !window.isMiniaturized {
            alert.beginSheetModal(for: window, completionHandler: done)
        } else {
            done(alert.runModal())
        }
    }

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let a = NSAlert()
        a.messageText = message
        a.addButton(withTitle: "好")
        presentAlert(a) { _ in completionHandler() }
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let a = NSAlert()
        a.messageText = message
        a.addButton(withTitle: "好")
        a.addButton(withTitle: "取消")
        presentAlert(a) { r in completionHandler(r == .alertFirstButtonReturn) }
    }

    func webView(_ webView: WKWebView, runJavaScriptTextInputPanelWithPrompt prompt: String, defaultText: String?,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (String?) -> Void) {
        let a = NSAlert()
        a.messageText = prompt
        let tf = NSTextField(frame: NSRect(x: 0, y: 0, width: 320, height: 24))
        tf.stringValue = defaultText ?? ""
        a.accessoryView = tf
        a.addButton(withTitle: "好")
        a.addButton(withTitle: "取消")
        a.window.initialFirstResponder = tf
        presentAlert(a) { r in completionHandler(r == .alertFirstButtonReturn ? tf.stringValue : nil) }
    }

    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        let p = NSOpenPanel()
        p.allowsMultipleSelection = parameters.allowsMultipleSelection
        p.canChooseDirectories = parameters.allowsDirectories
        p.canChooseFiles = true
        p.beginSheetModal(for: window) { r in completionHandler(r == .OK ? p.urls : nil) }
    }

    // MARK: WKDownloadDelegate（保存到 ~/Downloads，文件名去重）
    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse, suggestedFilename: String,
                  completionHandler: @escaping (URL?) -> Void) {
        let dir = cfg.downloadDir
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let dest = uniqueDestination(dir: dir, name: sanitizeFilename(suggestedFilename))
        downloads[ObjectIdentifier(download)] = dest
        NativeLog.write("下载：\(dest.path)")
        completionHandler(dest)
    }

    func downloadDidFinish(_ download: WKDownload) {
        if let dest = downloads.removeValue(forKey: ObjectIdentifier(download)) {
            // 让程序坞里的"下载"叠放跳一下（与 Safari 相同）
            DistributedNotificationCenter.default().post(name: Notification.Name("com.apple.DownloadFileFinished"),
                                                         object: dest.resolvingSymlinksInPath().path)
            #if CCR_SELFTEST
            selfTest?.downloaded.append(dest.path)
            #endif
        }
    }

    func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
        let dest = downloads.removeValue(forKey: ObjectIdentifier(download))
        NativeLog.write("下载失败：\(error.localizedDescription)")
        let a = NSAlert()
        a.messageText = "下载失败"
        a.informativeText = (dest.map { $0.lastPathComponent + "：" } ?? "") + error.localizedDescription
        a.addButton(withTitle: "好")
        presentAlert(a) { _ in }
    }
}

// MARK: - 系统通知（UNUserNotificationCenter，SPEC §14.12）
// 页面（static/sysnotify.js）决定什么时候通知：复用后端与提示音同一个判定的 meta.done_at / meta.attn_at（不另造一套），
// 经**页面世界**的 `ccrNotify` handler（WKScriptMessageHandlerWithReply，页面拿到 Promise）交给这里：
// 只接受主框架、本应用 origin、当前显示本应用页面时的消息 → 字段校验（ccrParseNotify）→ 开关（UserDefaults CCRSysNotify，
// 默认关）→ 「当前会话 + 窗口在前台」不弹 → 每分钟最多 kMaxNotifyPerMinute 条 → UNUserNotificationCenter。
// - 授权只在用户第一次打开开关时请求（op=enable）；启动时只在开关已经打开过时设 delegate（不请求、不弹框）。
// - 同一会话用同一个 identifier（ccr.<sid>）：新的替换旧的，通知中心里每个会话只留一条；不带声音（提示音由后端 afplay 负责）。
// - 点通知 → 激活窗口 → 页面派发 ccr-native-open-session（页面还没准备好就先记下，等它 op=status 时取走，60 秒内有效）。
// - 不读写剪贴板、不碰 ~/.claude*；日志只记失败原因，不记通知内容。

let kSysNotifyKey = "CCRSysNotify"
let kMaxNotifyPerMinute = 30
let kOpenPendingTTL: TimeInterval = 60

// BEGIN ccr-notify-script（tests/test_sysnotify.py 抽出这段单独编译，核对校验规则与事件脚本）
struct CCRNotifyRequest {
    let sid: String
    let kind: String      // done | attention
    let title: String
    let body: String
    let current: Bool     // 页面当前打开的就是这个会话（窗口在前台时不弹）
}

/// 会话 id：与 ccr/server.py 的 SID_RE 相同（[A-Za-z0-9_.-]{1,80}）
func ccrValidSid(_ s: String) -> Bool {
    let u = Array(s.utf8)
    guard (1...80).contains(u.count) else { return false }
    return u.allSatisfy { c in
        (c >= 48 && c <= 57) || (c >= 65 && c <= 90) || (c >= 97 && c <= 122) || c == 95 || c == 46 || c == 45
    }
}

/// 控制字符 / 换行 / U+2028/2029 换成空格、合并空白、去首尾空白，超过 max 个字符截断加 …
func ccrCleanText(_ s: String, max: Int) -> String {
    var out = String.UnicodeScalarView()
    var space = true
    for ch in s.unicodeScalars {
        let v = ch.value
        if v < 0x20 || (v >= 0x7f && v <= 0x9f) || v == 0x2028 || v == 0x2029 || CharacterSet.whitespacesAndNewlines.contains(ch) {
            if !space { out.append(" "); space = true }
        } else {
            out.append(ch)
            space = false
        }
    }
    var t = String(out).trimmingCharacters(in: .whitespaces)
    if t.count > max { t = String(t.prefix(Swift.max(0, max - 1))).trimmingCharacters(in: .whitespaces) + "…" }
    return t
}

/// 页面发来的 op=post 消息 → 校验后的请求；字段缺失 / 类型不对 / 太长 → nil
func ccrParseNotify(_ raw: Any?) -> CCRNotifyRequest? {
    guard let b = raw as? [String: Any], b.count <= 8,
          let sid = b["sid"] as? String, ccrValidSid(sid),
          let kind = b["kind"] as? String, kind == "done" || kind == "attention",
          let title = b["title"] as? String, title.utf16.count <= 2000,
          let body = b["body"] as? String, body.utf16.count <= 2000 else { return nil }
    var current = false
    if let c = b["current"] {
        guard let v = c as? Bool else { return nil }
        current = v
    }
    let t = ccrCleanText(title, max: 120)
    let x = ccrCleanText(body, max: 160)
    return CCRNotifyRequest(sid: sid, kind: kind, title: t.isEmpty ? "会话" : t,
                            body: x.isEmpty ? (kind == "done" ? "完成" : "需要你处理") : x, current: current)
}

/// 点了通知 → 交给页面打开会话的脚本（JSON 由 JSONSerialization 生成，sid 只是字符串内容）。
/// 结果 = 页面的监听者是否接手了（它会 preventDefault）；没接手（页面还在加载）时由调用方先记下。
func ccrOpenSessionScript(_ sid: String) -> String? {
    guard ccrValidSid(sid), let data = try? JSONSerialization.data(withJSONObject: ["sid": sid]),
          let json = String(data: data, encoding: .utf8) else { return nil }
    return "(function () { var e = new CustomEvent('ccr-native-open-session', { detail: \(json), cancelable: true });"
        + " return !window.dispatchEvent(e); })();"
}
// END ccr-notify-script

/// UNUserNotificationCenter 的回调在任意线程：转到主线程交给 SysNotifier
final class SysNotifyDelegate: NSObject, UNUserNotificationCenterDelegate {
    var onOpen: ((String) -> Void)?

    // 窗口在前台时系统默认不显示横幅：页面已经判断过（不是当前会话才会发），这里照样显示
    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .list])
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                withCompletionHandler completionHandler: @escaping () -> Void) {
        let sid = response.notification.request.content.userInfo["sid"] as? String
        if response.actionIdentifier == UNNotificationDefaultActionIdentifier, let sid = sid, ccrValidSid(sid) {
            let cb = onOpen
            DispatchQueue.main.async { cb?(sid) }
        }
        completionHandler()
    }
}

@MainActor
final class SysNotifier: NSObject, WKScriptMessageHandlerWithReply {
    weak var controller: AppController?
    let delegate = SysNotifyDelegate()
    var pendingOpen: (sid: String, at: Date)?
    var sentTimes: [Date] = []
    var lastError: String?

    /// 正常安装的 .app（有 bundle id）才能用 UNUserNotificationCenter；否则 current() 会直接抛异常让进程崩溃
    static var supported: Bool {
        return Bundle.main.bundleIdentifier != nil && Bundle.main.bundleURL.pathExtension == "app"
    }

    var enabled: Bool {
        get { UserDefaults.standard.bool(forKey: kSysNotifyKey) }
        set { UserDefaults.standard.set(newValue, forKey: kSysNotifyKey) }
    }

    /// 启动时调用：开关打开过才设 delegate（点通知能打开会话）。不请求授权、不弹任何框
    func setUp(_ c: AppController) {
        controller = c
        delegate.onOpen = { [weak self] sid in
            MainActor.assumeIsolated { self?.open(sid) }
        }
        if Self.supported && enabled { UNUserNotificationCenter.current().delegate = delegate }
    }

    nonisolated static func authName(_ s: UNAuthorizationStatus) -> String {
        switch s {
        case .notDetermined: return "notDetermined"
        case .denied: return "denied"
        case .authorized: return "authorized"
        case .provisional: return "provisional"
        default: return "authorized"   // ephemeral 等（macOS 上不会出现）
        }
    }

    // MARK: 页面消息（只接受主框架、本应用 origin、正在显示本应用页面）
    func userContentController(_ ucc: WKUserContentController, didReceive message: WKScriptMessage,
                               replyHandler: @escaping @MainActor @Sendable (Any?, String?) -> Void) {
        guard let c = controller, message.frameInfo.isMainFrame, c.pageState == .app,
              c.isAppOrigin(message.frameInfo.securityOrigin),
              let body = message.body as? [String: Any], body.count <= 8, let op = body["op"] as? String else {
            replyHandler(nil, "rejected")
            return
        }
        switch op {
        case "status":
            status { replyHandler($0, nil) }
        case "enable":
            guard let on = body["on"] as? Bool else { return replyHandler(nil, "bad request") }
            enable(on) { replyHandler($0, nil) }
        case "post":
            guard let req = ccrParseNotify(body) else { return replyHandler(nil, "bad request") }
            post(req) { replyHandler($0, nil) }
        case "clear":
            guard let sid = body["sid"] as? String, ccrValidSid(sid) else { return replyHandler(nil, "bad request") }
            if Self.supported && enabled {
                UNUserNotificationCenter.current().removeDeliveredNotifications(withIdentifiers: ["ccr." + sid])
            }
            replyHandler(["ok": true], nil)
        default:
            replyHandler(nil, "unknown op")
        }
    }

    /// {available, enabled, auth, open?, error?}；open = 点通知时页面还没准备好、记下来的会话（取走即清）
    func status(_ done: @escaping @MainActor ([String: Any]) -> Void) {
        var open: String? = nil
        if let p = pendingOpen {
            if Date().timeIntervalSince(p.at) < kOpenPendingTTL { open = p.sid }
            pendingOpen = nil
        }
        let on = enabled
        let err = lastError
        guard Self.supported else {
            done(["available": false, "enabled": false, "auth": "unavailable", "open": open ?? NSNull()])
            return
        }
        UNUserNotificationCenter.current().getNotificationSettings { s in
            let auth = Self.authName(s.authorizationStatus)
            DispatchQueue.main.async {
                var r: [String: Any] = ["available": true, "enabled": on && auth != "denied", "auth": auth,
                                        "open": open ?? NSNull()]
                if let e = err { r["error"] = e }
                done(r)
            }
        }
    }

    /// 打开 / 关闭开关。打开且从没问过 → 这时才请求授权（系统弹「是否允许通知」）；被拒绝过 → 保持关闭（页面提示去系统设置）
    func enable(_ on: Bool, _ done: @escaping @MainActor ([String: Any]) -> Void) {
        lastError = nil
        guard Self.supported else { enabled = false; return status(done) }
        if !on {
            enabled = false
            NativeLog.write("系统通知：已关闭")
            return status(done)
        }
        UNUserNotificationCenter.current().delegate = delegate
        UNUserNotificationCenter.current().getNotificationSettings { s in
            let st = s.authorizationStatus
            DispatchQueue.main.async {
                switch st {
                case .notDetermined:
                    NativeLog.write("系统通知：用户打开了开关，请求授权")
                    UNUserNotificationCenter.current().requestAuthorization(options: [.alert]) { granted, error in
                        let msg = error?.localizedDescription
                        DispatchQueue.main.async {
                            NativeLog.write("系统通知：授权结果 = \(granted ? "允许" : "不允许")\(msg.map { "（\($0)）" } ?? "")")
                            self.enabled = granted
                            self.lastError = granted ? nil : msg
                            self.status(done)
                        }
                    }
                case .denied:
                    self.enabled = false
                    self.status(done)
                default:
                    self.enabled = true
                    NativeLog.write("系统通知：已打开")
                    self.status(done)
                }
            }
        }
    }

    func post(_ req: CCRNotifyRequest, _ done: @escaping @MainActor ([String: Any]) -> Void) {
        guard Self.supported, enabled else { return done(["ok": false, "reason": "disabled"]) }
        if req.current, let c = controller, c.isForeground() { return done(["ok": false, "reason": "foreground"]) }
        let now = Date()
        sentTimes = sentTimes.filter { now.timeIntervalSince($0) < 60 }
        guard sentTimes.count < kMaxNotifyPerMinute else { return done(["ok": false, "reason": "rate"]) }
        sentTimes.append(now)
        let content = UNMutableNotificationContent()
        content.title = req.title
        content.body = req.body
        content.threadIdentifier = req.sid
        content.userInfo = ["sid": req.sid, "kind": req.kind]
        let request = UNNotificationRequest(identifier: "ccr." + req.sid, content: content, trigger: nil)
        UNUserNotificationCenter.current().add(request) { error in
            let msg = error?.localizedDescription
            DispatchQueue.main.async {
                if let m = msg { NativeLog.write("系统通知发送失败：\(m)") }
                done(msg == nil ? ["ok": true] : ["ok": false, "reason": "error"])
            }
        }
    }

    /// 点了通知：窗口提到前面，交给页面打开会话；页面还没准备好（加载中 / 刚启动）就先记下
    func open(_ sid: String) {
        guard ccrValidSid(sid), let c = controller else { return }
        NativeLog.write("系统通知：点了通知，打开会话")
        c.showWindow()
        guard c.pageState == .app, !c.webView.isLoading, let js = ccrOpenSessionScript(sid) else {
            pendingOpen = (sid, Date())
            return
        }
        c.webView.evaluateJavaScript(js) { [weak self] result, _ in
            MainActor.assumeIsolated {
                if (result as? Bool) != true { self?.pendingOpen = (sid, Date()) }
            }
        }
    }
}

func sanitizeFilename(_ name: String) -> String {
    var s = (name as NSString).lastPathComponent
    s = String(s.unicodeScalars.map { c -> Character in
        if c.value < 0x20 || c == "/" || c == ":" || c == "\\" { return "_" }
        return Character(c)
    })
    while s.hasPrefix(".") || s.hasPrefix(" ") { s.removeFirst() }
    s = s.trimmingCharacters(in: .whitespaces)
    if s.count > 200 { s = String(s.suffix(200)) }
    return s.isEmpty ? "下载" : s
}

func uniqueDestination(dir: URL, name: String) -> URL {
    let fm = FileManager.default
    var cand = dir.appendingPathComponent(name)
    let base = (name as NSString).deletingPathExtension
    let ext = (name as NSString).pathExtension
    var i = 1
    while fm.fileExists(atPath: cand.path) || (try? fm.destinationOfSymbolicLink(atPath: cand.path)) != nil {
        cand = dir.appendingPathComponent(ext.isEmpty ? "\(base) (\(i))" : "\(base) (\(i)).\(ext)")
        i += 1
    }
    return cand
}

// MARK: - 自测钩子（仅 -D CCR_SELFTEST 编译；正式版没有这段代码）

#if CCR_SELFTEST
/// <数据目录>/selftest/cmd-<n>.json → 执行 → res-<n>.json。只在自测版里存在：外部链接只记录不打开；
/// 不提供读写系统剪贴板的操作（新版 macOS 对程序化读取剪贴板有隐私提示，也不该覆盖用户的剪贴板）。
@MainActor
final class SelfTest {
    unowned let c: AppController
    let dir: URL
    var timer: Timer?
    var external: [String] = []
    var downloaded: [String] = []

    init(controller: AppController) {
        c = controller
        dir = controller.cfg.dataDir.appendingPathComponent("selftest", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        timer = Timer.scheduledTimer(withTimeInterval: 0.2, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.poll() }
        }
    }

    func recordExternal(_ u: URL) -> Bool {
        external.append(u.absoluteString)
        return true
    }

    func poll() {
        guard let names = try? FileManager.default.contentsOfDirectory(atPath: dir.path) else { return }
        for n in names.sorted() where n.hasPrefix("cmd-") && n.hasSuffix(".json") {
            let f = dir.appendingPathComponent(n)
            guard let d = try? Data(contentsOf: f) else { continue }
            try? FileManager.default.removeItem(at: f)
            let id = String(n.dropFirst(4).dropLast(5))
            guard let cmd = (try? JSONSerialization.jsonObject(with: d)) as? [String: Any] else {
                reply(id, ["error": "bad json"]); continue
            }
            run(cmd) { [weak self] r in self?.reply(id, r) }
        }
    }

    func reply(_ id: String, _ r: [String: Any]) {
        let safe = JSONSerialization.isValidJSONObject(r) ? r : ["result": String(describing: r)]
        if let d = try? JSONSerialization.data(withJSONObject: safe, options: [.sortedKeys]) {
            let tmp = dir.appendingPathComponent(".res-\(id).tmp")
            try? d.write(to: tmp)
            rename(tmp.path, dir.appendingPathComponent("res-\(id).json").path)
        }
    }

    func jsonable(_ v: Any?) -> Any {
        guard let v = v else { return NSNull() }
        if JSONSerialization.isValidJSONObject(["v": v]) { return v }
        return String(describing: v)
    }

    func menuTree(_ m: NSMenu?) -> [[String: Any]] {
        guard let m = m else { return [] }
        return m.items.map { it in
            var d: [String: Any] = ["title": it.title, "key": it.keyEquivalent,
                                    "mods": Int(it.keyEquivalentModifierMask.rawValue),
                                    "sep": it.isSeparatorItem]
            if let a = it.action { d["action"] = NSStringFromSelector(a) }
            if let s = it.submenu { d["items"] = menuTree(s) }
            return d
        }
    }

    func findItem(_ path: [String]) -> NSMenuItem? {
        var menu = NSApp.mainMenu
        var found: NSMenuItem? = nil
        for t in path {
            found = menu?.items.first(where: { $0.title == t })
            menu = found?.submenu
        }
        return found
    }

    func viewPoint(x: Double, y: Double) -> NSPoint {
        let z = Double(c.webView.pageZoom)
        let wv = c.webView!
        let p = NSPoint(x: x * z, y: wv.isFlipped ? y * z : Double(wv.bounds.height) - y * z)
        return wv.convert(p, to: nil)
    }

    func run(_ cmd: [String: Any], _ done: @escaping ([String: Any]) -> Void) {
        let op = cmd["op"] as? String ?? ""
        switch op {
        case "eval":
            c.webView.callAsyncJavaScript(cmd["js"] as? String ?? "", arguments: [:], in: nil, in: .page) { r in
                switch r {
                case .success(let v): done(["ok": true, "value": self.jsonable(v)])
                case .failure(let e): done(["ok": false, "error": String(describing: e)])
                }
            }
        case "state":
            let app = NSRunningApplication.current
            done(["title": c.window.title, "visible": c.window.isVisible, "mini": c.window.isMiniaturized,
                  "key": c.window.isKeyWindow, "active": NSApp.isActive, "zoom": Double(c.webView.pageZoom),
                  "url": c.webView.url?.absoluteString ?? "", "pageState": "\(c.pageState)",
                  "knownPid": c.knownPid.map { Int($0) } ?? -1, "connecting": c.connecting, "authLost": c.authLost,
                  "localizedName": app.localizedName ?? "", "bundleId": app.bundleIdentifier ?? "",
                  "bundleName": Bundle.main.infoDictionary?["CFBundleName"] as? String ?? "",
                  "windowNumber": c.window.windowNumber,
                  "frame": NSStringFromRect(c.window.frame), "fullscreen": c.window.styleMask.contains(.fullScreen),
                  "external": external, "downloaded": downloaded,
                  "windowsMenu": (NSApp.windowsMenu?.items ?? []).map { $0.title },
                  "menu": menuTree(NSApp.mainMenu), "pid": Int(getpid())])
        case "menu":
            guard let it = findItem(cmd["path"] as? [String] ?? []), let m = it.menu else { return done(["ok": false]) }
            m.update()
            let enabled = it.isEnabled
            m.performActionForItem(at: m.index(of: it))
            done(["ok": true, "enabled": enabled])
        case "key":
            // 与真实按键同一路径：NSApp.sendEvent → 窗口/WKWebView → 主菜单 key equivalent
            let chars = cmd["chars"] as? String ?? ""
            var flags: NSEvent.ModifierFlags = []
            for m in cmd["mods"] as? [String] ?? [] {
                switch m {
                case "cmd": flags.insert(.command)
                case "shift": flags.insert(.shift)
                case "ctrl": flags.insert(.control)
                case "opt": flags.insert(.option)
                default: break
                }
            }
            let code = UInt16(cmd["keyCode"] as? Int ?? 0)
            for t in [NSEvent.EventType.keyDown, .keyUp] {
                if let e = NSEvent.keyEvent(with: t, location: .zero, modifierFlags: flags,
                                            timestamp: ProcessInfo.processInfo.systemUptime,
                                            windowNumber: c.window.windowNumber, context: nil, characters: chars,
                                            charactersIgnoringModifiers: cmd["ign"] as? String ?? chars,
                                            isARepeat: false, keyCode: code) {
                    NSApp.sendEvent(e)
                }
            }
            done(["ok": true])
        case "click":
            let p = viewPoint(x: cmd["x"] as? Double ?? 0, y: cmd["y"] as? Double ?? 0)
            for t in [NSEvent.EventType.leftMouseDown, .leftMouseUp] {
                if let e = NSEvent.mouseEvent(with: t, location: p, modifierFlags: [],
                                              timestamp: ProcessInfo.processInfo.systemUptime,
                                              windowNumber: c.window.windowNumber, context: nil, eventNumber: 0,
                                              clickCount: 1, pressure: t == .leftMouseDown ? 1 : 0) {
                    c.window.sendEvent(e)
                }
            }
            done(["ok": true, "winPoint": NSStringFromPoint(p)])
        case "activate":
            c.showWindow()
            done(["ok": true])
        case "close":
            c.window.performClose(nil)
            done(["ok": true])
        case "reopen":
            _ = c.applicationShouldHandleReopen(NSApp, hasVisibleWindows: c.window.isVisible)
            done(["ok": true])
        case "becomeActive":
            c.applicationDidBecomeActive(Notification(name: NSApplication.didBecomeActiveNotification))
            done(["ok": true])
        case "miniaturize":
            c.window.miniaturize(nil)
            done(["ok": true])
        case "snapshot":
            let path = cmd["path"] as? String ?? "/tmp/ccr-snap.png"
            c.webView.takeSnapshot(with: nil) { img, err in
                guard let img = img, let tiff = img.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff),
                      let png = rep.representation(using: .png, properties: [:]) else {
                    return done(["ok": false, "error": String(describing: err)])
                }
                try? png.write(to: URL(fileURLWithPath: path))
                done(["ok": true, "path": path])
            }
        case "webPid":   // 网页进程 pid（私有属性，仅自测版；外部 kill -9 它来模拟真实崩溃）
            let n = (c.webView.value(forKey: "_webProcessIdentifier") as? NSNumber)?.intValue ?? -1
            done(["ok": n > 0, "pid": n])
        case "quit":
            done(["ok": true])
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) { NSApp.terminate(nil) }
        default:
            done(["ok": false, "error": "unknown op \(op)"])
        }
    }
}
#endif

// MARK: - 入口

MainActor.assumeIsolated {
    let app = NSApplication.shared
    app.setActivationPolicy(.regular)
    let controller = AppController()
    app.delegate = controller
    app.run()
}
