using System;
using System.Diagnostics;
using System.IO;
using System.Management;
using System.Net;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;

namespace EgonSupervisor
{
    internal static class Program
    {
        private const string MutexName = "Global\\EgonSupervisor";
        private static string _root = "";
        private static string _logPath = "";
        private static string _healthPath = "";

        private static int Main(string[] args)
        {
            bool once = false;
            _root = AppDomain.CurrentDomain.BaseDirectory;

            for (int i = 0; i < args.Length; i++)
            {
                if (args[i].Equals("--once", StringComparison.OrdinalIgnoreCase))
                {
                    once = true;
                }
                else if (args[i].Equals("--root", StringComparison.OrdinalIgnoreCase) && i + 1 < args.Length)
                {
                    _root = args[++i];
                    while (i + 1 < args.Length && !args[i + 1].StartsWith("--", StringComparison.Ordinal))
                    {
                        _root += " " + args[++i];
                    }
                }
            }

            _root = Path.GetFullPath(_root);
            _logPath = Path.Combine(_root, "logs", "egon-supervisor.log");
            _healthPath = Path.Combine(_root, "state", "supervisor_health.json");
            Directory.CreateDirectory(Path.GetDirectoryName(_logPath) ?? _root);
            Directory.CreateDirectory(Path.GetDirectoryName(_healthPath) ?? _root);

            bool created;
            using (var mutex = new Mutex(false, MutexName, out created))
            {
                if (!created)
                {
                    Log("info", "already_running");
                    return once ? 0 : 1;
                }

                Log("info", "supervisor_start", "root=" + _root, "once=" + once);
                do
                {
                    Tick();
                    if (once) break;
                    Thread.Sleep(TimeSpan.FromSeconds(30));
                } while (true);
            }

            return 0;
        }

        private static void Tick()
        {
            bool coreRunning = CountRole("scripts\\egon_core.py") > 0 || CountRole("scripts/egon_core.py") > 0;
            if (!coreRunning)
            {
                StartCore();
            }

            bool mindOk = MindOk();
            string indexPath;
            string indexReason;
            bool indexOk = ConnectIndexOk(out indexPath, out indexReason);
            Log("info", "tick", "core_running=" + coreRunning, "mind_ok=" + mindOk,
                "connect_index_ok=" + indexOk, "connect_index_reason=" + Sanitize(indexReason),
                "connect_index_path=" + Sanitize(indexPath));
            WriteHealth(coreRunning, mindOk, indexOk, indexReason, indexPath);
        }

        private static void StartCore()
        {
            string py = ResolveBasePython(windowed: true);
            string site = Path.Combine(_root, ".venv", "Lib", "site-packages");
            string script = Path.Combine(_root, "scripts", "egon_core.py");
            if (!File.Exists(script))
            {
                Log("error", "core_script_missing", "path=" + script);
                return;
            }

            var psi = new ProcessStartInfo
            {
                FileName = py,
                Arguments = Quote(script),
                WorkingDirectory = _root,
                UseShellExecute = false,
                CreateNoWindow = true,
            };
            psi.EnvironmentVariables["PYTHONDONTWRITEBYTECODE"] = "1";
            psi.EnvironmentVariables["PYTHONPATH"] = site + ";" + (psi.EnvironmentVariables["PYTHONPATH"] ?? "");

            try
            {
                Process.Start(psi);
                Log("info", "core_start_requested", "python=" + py);
            }
            catch (Exception ex)
            {
                Log("error", "core_start_failed", "error=" + ex.GetType().Name + ":" + ex.Message);
            }
        }

        private static string ResolveBasePython(bool windowed)
        {
            string venv = Path.Combine(_root, ".venv");
            string cfg = Path.Combine(venv, "pyvenv.cfg");
            string exe = windowed ? "pythonw.exe" : "python.exe";
            try
            {
                foreach (string raw in File.ReadAllLines(cfg))
                {
                    string compact = raw.ToLowerInvariant().Replace(" ", "");
                    if (!compact.StartsWith("home=")) continue;
                    string home = raw.Substring(raw.IndexOf('=') + 1).Trim();
                    string candidate = Path.Combine(home, exe);
                    if (File.Exists(candidate)) return candidate;
                }
            }
            catch
            {
            }

            string fallback = Path.Combine(venv, "Scripts", exe);
            return File.Exists(fallback) ? fallback : exe;
        }

        private static int CountRole(string token)
        {
            int count = 0;
            string escaped = token.Replace("\\", "\\\\").Replace("'", "''");
            string query = "SELECT ProcessId, CommandLine FROM Win32_Process " +
                           "WHERE (Name='python.exe' OR Name='pythonw.exe') " +
                           "AND CommandLine LIKE '%" + escaped + "%'";
            try
            {
                using (var searcher = new ManagementObjectSearcher(query))
                using (var results = searcher.Get())
                {
                    foreach (ManagementObject _ in results) count++;
                }
            }
            catch (Exception ex)
            {
                Log("warn", "process_query_failed", "error=" + ex.GetType().Name + ":" + ex.Message);
            }
            return count;
        }

        private static bool MindOk()
        {
            try
            {
                var req = (HttpWebRequest)WebRequest.Create("http://127.0.0.1:8000/api/v1/mind/stats");
                req.Timeout = 2500;
                using (var resp = (HttpWebResponse)req.GetResponse())
                {
                    return (int)resp.StatusCode >= 200 && (int)resp.StatusCode < 300;
                }
            }
            catch
            {
                return false;
            }
        }

        private static bool ConnectIndexOk(out string indexPath, out string reason)
        {
            indexPath = ResolveConnectIndexDir();
            reason = "ok";
            if (string.IsNullOrEmpty(indexPath) || !Directory.Exists(indexPath))
            {
                reason = "missing_dir";
                return false;
            }

            string complete = Path.Combine(indexPath, "COMPLETE.json");
            string vectors = Path.Combine(indexPath, "vectors.npy");
            string meta = Path.Combine(indexPath, "meta.json");
            string turbo = Path.Combine(indexPath, "turbo.idx");
            string model = Path.Combine(indexPath, "model.json");

            foreach (string file in new[] { complete, vectors, meta, turbo, model })
            {
                if (!File.Exists(file))
                {
                    reason = "missing_" + Path.GetFileName(file);
                    return false;
                }
                if (new FileInfo(file).Length <= 0)
                {
                    reason = "empty_" + Path.GetFileName(file);
                    return false;
                }
            }

            int items;
            int dim;
            if (!ReadCompleteShape(complete, out items, out dim))
            {
                reason = "bad_COMPLETE.json";
                return false;
            }

            int rows;
            int cols;
            if (!ReadNpyShape(vectors, out rows, out cols))
            {
                reason = "bad_vectors_header";
                return false;
            }

            if (items != rows || dim != cols)
            {
                reason = "shape_mismatch_complete=" + items + "x" + dim + "_vectors=" + rows + "x" + cols;
                return false;
            }

            return true;
        }

        private static string ResolveConnectIndexDir()
        {
            string env = Environment.GetEnvironmentVariable("EGON_CONNECT_INDEX_DIR");
            if (!string.IsNullOrWhiteSpace(env)) return env;

            string[] candidates = new[]
            {
                @"G:\My Drive\EgonData\connect_index",
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), "Google Drive", "EgonData", "connect_index"),
                Path.Combine(_root, "state", "connect_index")
            };
            foreach (string candidate in candidates)
            {
                if (Directory.Exists(candidate)) return candidate;
            }
            return candidates[candidates.Length - 1];
        }

        private static bool ReadCompleteShape(string path, out int items, out int dim)
        {
            items = 0;
            dim = 0;
            try
            {
                string text = File.ReadAllText(path);
                Match mi = Regex.Match(text, "\"items\"\\s*:\\s*(\\d+)");
                Match md = Regex.Match(text, "\"dim\"\\s*:\\s*(\\d+)");
                if (!mi.Success || !md.Success) return false;
                items = int.Parse(mi.Groups[1].Value);
                dim = int.Parse(md.Groups[1].Value);
                return items > 0 && dim > 0;
            }
            catch
            {
                return false;
            }
        }

        private static bool ReadNpyShape(string path, out int rows, out int cols)
        {
            rows = 0;
            cols = 0;
            try
            {
                using (var fs = File.OpenRead(path))
                using (var br = new BinaryReader(fs))
                {
                    byte[] magic = br.ReadBytes(6);
                    if (magic.Length != 6 || magic[0] != 0x93 || magic[1] != (byte)'N') return false;
                    byte major = br.ReadByte();
                    br.ReadByte(); // minor
                    int headerLen = major <= 1 ? br.ReadUInt16() : (int)br.ReadUInt32();
                    string header = Encoding.ASCII.GetString(br.ReadBytes(headerLen));
                    Match shape = Regex.Match(header, "\\((\\d+)\\s*,\\s*(\\d+)\\s*\\)");
                    if (!shape.Success) return false;
                    rows = int.Parse(shape.Groups[1].Value);
                    cols = int.Parse(shape.Groups[2].Value);
                    return rows > 0 && cols > 0;
                }
            }
            catch
            {
                return false;
            }
        }

        private static void WriteHealth(bool coreRunning, bool mindOk, bool indexOk,
                                        string indexReason, string indexPath)
        {
            string json = "{\n" +
                "  \"status\": \"" + ((coreRunning && mindOk && indexOk) ? "ok" : "degraded") + "\",\n" +
                "  \"at\": \"" + JsonEscape(DateTime.Now.ToString("s")) + "\",\n" +
                "  \"core_running\": " + Bool(coreRunning) + ",\n" +
                "  \"mind_ok\": " + Bool(mindOk) + ",\n" +
                "  \"connect_index_ok\": " + Bool(indexOk) + ",\n" +
                "  \"connect_index_reason\": \"" + JsonEscape(indexReason) + "\",\n" +
                "  \"connect_index_path\": \"" + JsonEscape(indexPath) + "\"\n" +
                "}\n";
            try
            {
                File.WriteAllText(_healthPath, json);
            }
            catch (Exception ex)
            {
                Log("warn", "health_write_failed", "error=" + ex.GetType().Name + ":" + ex.Message);
            }
        }

        private static string Quote(string value)
        {
            return "\"" + value.Replace("\"", "\\\"") + "\"";
        }

        private static string Bool(bool value)
        {
            return value ? "true" : "false";
        }

        private static string Sanitize(string value)
        {
            return (value ?? "").Replace(" ", "_");
        }

        private static string JsonEscape(string value)
        {
            return (value ?? "").Replace("\\", "\\\\").Replace("\"", "\\\"");
        }

        private static void Log(string level, string evt, params string[] fields)
        {
            string line = DateTime.Now.ToString("s") + " [" + level + "] event=" + evt;
            if (fields != null && fields.Length > 0)
            {
                line += " " + string.Join(" ", fields);
            }
            try
            {
                File.AppendAllText(_logPath, line + Environment.NewLine);
            }
            catch
            {
            }
        }
    }
}
