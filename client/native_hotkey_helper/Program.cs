using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text.Json;
using System.Threading;

internal sealed class HotkeySpec
{
    public string Name { get; }
    public int? BaseVk { get; }
    public HashSet<int> Modifiers { get; }

    public HotkeySpec(string name, int? baseVk, IEnumerable<int> modifiers)
    {
        Name = name;
        BaseVk = baseVk;
        Modifiers = modifiers.ToHashSet();
    }

    public bool IsMatch(HashSet<int> pressedKeys)
        => (BaseVk is null || pressedKeys.Contains(BaseVk.Value)) && Modifiers.All(modifier => Program.IsModifierPressed(pressedKeys, modifier));

    public bool UsesKey(int vk)
        => (BaseVk is not null && BaseVk.Value == vk) || Modifiers.Any(modifier => Program.ModifierMatchesVk(modifier, vk));
}

internal static class Program
{
    private const int WH_KEYBOARD_LL = 13;
    private const int WM_KEYDOWN = 0x0100;
    private const int WM_KEYUP = 0x0101;
    private const int WM_SYSKEYDOWN = 0x0104;
    private const int WM_SYSKEYUP = 0x0105;

    private const int VK_SHIFT = 0x10;
    private const int VK_CONTROL = 0x11;
    private const int VK_MENU = 0x12;
    private const int VK_LWIN = 0x5B;
    private const int VK_RWIN = 0x5C;

    private static readonly Dictionary<string, int[]> ModifierNames = new(StringComparer.OrdinalIgnoreCase)
    {
        ["ctrl"] = new[] { VK_CONTROL },
        ["control"] = new[] { VK_CONTROL },
        ["strg"] = new[] { VK_CONTROL },
        ["left ctrl"] = new[] { 0xA2 },
        ["right ctrl"] = new[] { 0xA3 },
        ["linke strg"] = new[] { 0xA2 },
        ["rechte strg"] = new[] { 0xA3 },
        ["shift"] = new[] { VK_SHIFT },
        ["umschalt"] = new[] { VK_SHIFT },
        ["left shift"] = new[] { 0xA0 },
        ["right shift"] = new[] { 0xA1 },
        ["linke umschalt"] = new[] { 0xA0 },
        ["rechte umschalt"] = new[] { 0xA1 },
        ["alt"] = new[] { VK_MENU },
        ["left alt"] = new[] { 0xA4 },
        ["right alt"] = new[] { 0xA5 },
        ["linkes alt"] = new[] { 0xA4 },
        ["rechtes alt"] = new[] { 0xA5 },
        ["altgr"] = new[] { 0xA5 },
        ["win"] = new[] { VK_LWIN, VK_RWIN },
        ["windows"] = new[] { VK_LWIN, VK_RWIN },
        ["left windows"] = new[] { VK_LWIN },
        ["right windows"] = new[] { VK_RWIN },
        ["linke windows"] = new[] { VK_LWIN },
        ["rechte windows"] = new[] { VK_RWIN },
    };

    private static readonly Dictionary<string, int> NamedKeys = new(StringComparer.OrdinalIgnoreCase)
    {
        ["space"] = 0x20,
        ["tab"] = 0x09,
        ["enter"] = 0x0D,
        ["return"] = 0x0D,
        ["esc"] = 0x1B,
        ["escape"] = 0x1B,
        ["backspace"] = 0x08,
        ["delete"] = 0x2E,
        ["del"] = 0x2E,
        ["insert"] = 0x2D,
        ["ins"] = 0x2D,
        ["home"] = 0x24,
        ["end"] = 0x23,
        ["page up"] = 0x21,
        ["pageup"] = 0x21,
        ["page down"] = 0x22,
        ["pagedown"] = 0x22,
        ["up"] = 0x26,
        ["down"] = 0x28,
        ["left"] = 0x25,
        ["right"] = 0x27,
    };

    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
    };

    private static readonly object EmitLock = new();

    private static IntPtr _hook = IntPtr.Zero;
    private static HookProc? _hookProc;
    private static readonly HashSet<int> PressedKeys = new();
    private static readonly Dictionary<string, bool> ActiveHotkeys = new(StringComparer.OrdinalIgnoreCase);
    private static List<HotkeySpec> _hotkeys = new();
    private static bool _debugRaw;

    internal static bool ModifierMatchesVk(int modifier, int vk)
    {
        return modifier switch
        {
            VK_CONTROL => vk is VK_CONTROL or 0xA2 or 0xA3,
            VK_SHIFT => vk is VK_SHIFT or 0xA0 or 0xA1,
            VK_MENU => vk is VK_MENU or 0xA4 or 0xA5,
            VK_LWIN => vk == VK_LWIN,
            VK_RWIN => vk == VK_RWIN,
            _ => modifier == vk,
        };
    }

    internal static bool IsModifierPressed(HashSet<int> pressedKeys, int modifier)
        => pressedKeys.Any(vk => ModifierMatchesVk(modifier, vk));

    [STAThread]
    private static int Main(string[] args)
    {
        try
        {
            var parsed = ParseArgs(args);
            if (parsed.TryGetValue("validate", out var validateHotkey))
            {
                TryParseHotkey("validate", validateHotkey, out _, out var error);
                Emit(new { type = "validate", ok = error is null, error });
                return error is null ? 0 : 1;
            }

            _debugRaw = parsed.ContainsKey("debug-raw");
            _hotkeys = BuildHotkeys(parsed);
            foreach (var hotkey in _hotkeys)
            {
                ActiveHotkeys[hotkey.Name] = false;
            }

            if (parsed.TryGetValue("parent-pid", out var parentPidRaw) && int.TryParse(parentPidRaw, out var parentPid))
            {
                StartParentWatch(parentPid);
            }

            _hookProc = HookCallback;
            _hook = SetHook(_hookProc);
            if (_hook == IntPtr.Zero)
            {
                Emit(new { type = "error", message = "Failed to install keyboard hook." });
                return Marshal.GetLastWin32Error();
            }

            Emit(new
            {
                type = "status",
                @event = "ready",
                hotkeys = _hotkeys.Select(h => h.Name).ToArray(),
            });

            MSG msg;
            while (GetMessage(out msg, IntPtr.Zero, 0, 0) > 0)
            {
                TranslateMessage(ref msg);
                DispatchMessage(ref msg);
            }

            return 0;
        }
        catch (Exception ex)
        {
            Emit(new { type = "error", message = ex.Message });
            return 1;
        }
        finally
        {
            if (_hook != IntPtr.Zero)
            {
                UnhookWindowsHookEx(_hook);
            }
        }
    }

    private static Dictionary<string, string> ParseArgs(string[] args)
    {
        var parsed = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        for (var i = 0; i < args.Length; i++)
        {
            var arg = args[i];
            if (!arg.StartsWith("--", StringComparison.Ordinal))
            {
                continue;
            }

            var key = arg[2..];
            if (i + 1 < args.Length && !args[i + 1].StartsWith("--", StringComparison.Ordinal))
            {
                parsed[key] = args[++i];
            }
            else
            {
                parsed[key] = "true";
            }
        }

        return parsed;
    }

    private static void StartParentWatch(int parentPid)
    {
        var thread = new Thread(() =>
        {
            while (true)
            {
                try
                {
                    using var parent = Process.GetProcessById(parentPid);
                    if (parent.HasExited)
                    {
                        Environment.Exit(0);
                    }
                }
                catch
                {
                    Environment.Exit(0);
                }

                Thread.Sleep(1000);
            }
        })
        {
            IsBackground = true,
            Name = "ParentWatch",
        };
        thread.Start();
    }

    private static List<HotkeySpec> BuildHotkeys(Dictionary<string, string> parsed)
    {
        var hotkeys = new List<HotkeySpec>();
        if (parsed.TryGetValue("ptt", out var ptt))
        {
            if (!TryParseHotkey("ptt", ptt, out var spec, out var error))
            {
                throw new InvalidOperationException(error);
            }
            hotkeys.Add(spec!);
        }

        if (parsed.TryGetValue("fast-ptt", out var fastPtt) && !string.IsNullOrWhiteSpace(fastPtt))
        {
            if (!TryParseHotkey("fast_ptt", fastPtt, out var spec, out var error))
            {
                throw new InvalidOperationException(error);
            }
            hotkeys.Add(spec!);
        }

        if (parsed.TryGetValue("undo", out var undo) && !string.IsNullOrWhiteSpace(undo))
        {
            if (!TryParseHotkey("undo", undo, out var spec, out var error))
            {
                throw new InvalidOperationException(error);
            }
            hotkeys.Add(spec!);
        }

        if (hotkeys.Count == 0)
        {
            throw new InvalidOperationException("No hotkeys configured.");
        }

        return hotkeys;
    }

    private static bool TryParseHotkey(string name, string combo, out HotkeySpec? spec, out string? error)
    {
        spec = null;
        error = null;

        var tokens = combo
            .Split('+', StringSplitOptions.TrimEntries | StringSplitOptions.RemoveEmptyEntries)
            .Select(t => t.Trim())
            .ToList();

        if (tokens.Count == 0)
        {
            error = "Hotkey cannot be empty.";
            return false;
        }

        var modifiers = new HashSet<int>();
        int? baseVk = null;

        foreach (var token in tokens)
        {
            if (ModifierNames.TryGetValue(token, out var modifierVks))
            {
                foreach (var modifierVk in modifierVks)
                {
                    modifiers.Add(modifierVk);
                }
                continue;
            }

            if (baseVk is not null)
            {
                error = "Only one non-modifier key is supported.";
                return false;
            }

            if (!TryParseBaseKey(token, out var vk))
            {
                error = $"Unknown key token: {token}";
                return false;
            }

            baseVk = vk;
        }

        if (baseVk is null && modifiers.Count < 2)
        {
            error = "Modifier-only hotkeys must include at least two modifiers.";
            return false;
        }

        spec = new HotkeySpec(name, baseVk, modifiers);
        return true;
    }

    private static bool TryParseBaseKey(string token, out int vk)
    {
        vk = 0;
        if (NamedKeys.TryGetValue(token, out vk))
        {
            return true;
        }

        if (token.Length >= 2 && (token[0] == 'f' || token[0] == 'F') && int.TryParse(token[1..], out var fn) && fn >= 1 && fn <= 24)
        {
            vk = 0x70 + (fn - 1);
            return true;
        }

        if (token.Length == 1)
        {
            var ch = token[0];
            if (char.IsLetter(ch))
            {
                vk = char.ToUpperInvariant(ch);
                return true;
            }

            if (char.IsDigit(ch))
            {
                vk = ch;
                return true;
            }

            var layout = GetKeyboardLayout(0);
            var mapped = VkKeyScanEx(ch, layout);
            if (mapped != -1)
            {
                vk = mapped & 0xFF;
                return true;
            }
        }

        return false;
    }

    private static IntPtr HookCallback(int nCode, IntPtr wParam, IntPtr lParam)
    {
        if (nCode >= 0)
        {
            var msg = wParam.ToInt32();
            var isDown = msg is WM_KEYDOWN or WM_SYSKEYDOWN;
            var isUp = msg is WM_KEYUP or WM_SYSKEYUP;

            if (isDown || isUp)
            {
                var info = Marshal.PtrToStructure<KBDLLHOOKSTRUCT>(lParam);
                var vk = unchecked((int)info.vkCode);

                if (isDown)
                {
                    PressedKeys.Add(vk);
                }
                else
                {
                    PressedKeys.Remove(vk);
                }

                if (_debugRaw)
                {
                    Emit(new
                    {
                        type = "raw",
                        @event = isDown ? "down" : "up",
                        vk,
                    });
                }

                var suppress = false;
                foreach (var hotkey in _hotkeys)
                {
                    var matched = hotkey.IsMatch(PressedKeys);
                    var wasActive = ActiveHotkeys[hotkey.Name];
                    if (matched && !wasActive)
                    {
                        ActiveHotkeys[hotkey.Name] = true;
                        Emit(new { type = "hotkey", name = hotkey.Name, @event = "down" });
                    }
                    else if (!matched && wasActive)
                    {
                        ActiveHotkeys[hotkey.Name] = false;
                        Emit(new { type = "hotkey", name = hotkey.Name, @event = "up" });
                    }

                    if (isDown && hotkey.UsesKey(vk) && (matched || wasActive))
                    {
                        suppress = true;
                    }
                }

                if (suppress)
                {
                    return new IntPtr(1);
                }
            }
        }

        return CallNextHookEx(_hook, nCode, wParam, lParam);
    }

    private static IntPtr SetHook(HookProc proc)
    {
        using var currentProcess = Process.GetCurrentProcess();
        using var currentModule = currentProcess.MainModule;
        var moduleHandle = GetModuleHandle(currentModule?.ModuleName);
        return SetWindowsHookEx(WH_KEYBOARD_LL, proc, moduleHandle, 0);
    }

    private static void Emit(object payload)
    {
        lock (EmitLock)
        {
            Console.WriteLine(JsonSerializer.Serialize(payload, JsonOptions));
            Console.Out.Flush();
        }
    }

    private delegate IntPtr HookProc(int nCode, IntPtr wParam, IntPtr lParam);

    [StructLayout(LayoutKind.Sequential)]
    private struct KBDLLHOOKSTRUCT
    {
        public uint vkCode;
        public uint scanCode;
        public uint flags;
        public uint time;
        public nuint dwExtraInfo;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct POINT
    {
        public int x;
        public int y;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct MSG
    {
        public IntPtr hwnd;
        public uint message;
        public UIntPtr wParam;
        public IntPtr lParam;
        public uint time;
        public POINT pt;
        public uint lPrivate;
    }

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern short VkKeyScanEx(char ch, IntPtr dwhkl);

    [DllImport("user32.dll")]
    private static extern IntPtr GetKeyboardLayout(uint idThread);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern IntPtr SetWindowsHookEx(int idHook, HookProc lpfn, IntPtr hmod, uint dwThreadId);

    [DllImport("user32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool UnhookWindowsHookEx(IntPtr hhk);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern IntPtr CallNextHookEx(IntPtr hhk, int nCode, IntPtr wParam, IntPtr lParam);

    [DllImport("kernel32.dll", CharSet = CharSet.Auto, SetLastError = true)]
    private static extern IntPtr GetModuleHandle(string? lpModuleName);

    [DllImport("user32.dll")]
    private static extern sbyte GetMessage(out MSG lpMsg, IntPtr hWnd, uint wMsgFilterMin, uint wMsgFilterMax);

    [DllImport("user32.dll")]
    private static extern bool TranslateMessage([In] ref MSG lpMsg);

    [DllImport("user32.dll")]
    private static extern IntPtr DispatchMessage([In] ref MSG lpmsg);
}
