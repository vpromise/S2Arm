// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 S2Arm contributors
// 本机控制口（Windows 版，协议同 launchers/macos/adapter ControlPort.swift，客户端同一份 launchers/macos/s2armctl.py）：
// 只听 127.0.0.1；每次启动随机令牌；端口与令牌写 <私有目录>/control.json。命令怎么做由界面层的处理函数定，这里只管收发。
using System.Net;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace S2Arm.Launcher.Protocol;

public sealed record ControlRequest(string Method, string Path, IReadOnlyDictionary<string, string> Headers, JsonElement Body)
{
    public string? Text(string key) =>
        Body.ValueKind == JsonValueKind.Object && Body.TryGetProperty(key, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() : null;

    public bool Flag(string key) =>
        Body.ValueKind == JsonValueKind.Object && Body.TryGetProperty(key, out var v) && v.ValueKind == JsonValueKind.True;

    public string[]? Strings(string key) =>
        Body.ValueKind == JsonValueKind.Object && Body.TryGetProperty(key, out var v) && v.ValueKind == JsonValueKind.Array
            ? v.EnumerateArray().Where(i => i.ValueKind == JsonValueKind.String).Select(i => i.GetString()!).ToArray() : null;

    /// <summary>头没收全或正文不够 Content-Length 返回 null（继续收）。正文不是 JSON 按空对象。</summary>
    public static ControlRequest? Parse(ReadOnlySpan<byte> data)
    {
        var end = data.IndexOf("\r\n\r\n"u8);
        if (end < 0) return null;
        var lines = Encoding.UTF8.GetString(data[..end]).Split("\r\n");
        var first = lines[0].Split(' ', StringSplitOptions.RemoveEmptyEntries);
        if (first.Length < 2) return null;
        var headers = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        foreach (var line in lines.Skip(1))
            if (line.IndexOf(':') is var i and > 0) headers[line[..i].Trim()] = line[(i + 1)..].Trim();
        var length = headers.TryGetValue("Content-Length", out var raw) && int.TryParse(raw, out var n) && n > 0 ? n : 0;
        var body = data[(end + 4)..];
        if (body.Length < length) return null;
        var parsed = JsonDocument.Parse("{}").RootElement;
        if (length > 0)
            try { parsed = JsonDocument.Parse(body[..length].ToArray()).RootElement; }
            catch (JsonException) { }
        return new(first[0], first[1].Split('?')[0], headers, parsed);
    }
}

public sealed record ControlReply(int Code, string Type, byte[] Body)
{
    // 错误文字是 s2armctl 原样打给 AI 和玩家看的，中文不转义
    private static readonly JsonSerializerOptions Readable = new() { Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping };

    public static ControlReply Json(object? value, int code = 200) =>
        new(code, "application/json; charset=utf-8", JsonSerializer.SerializeToUtf8Bytes(value, Readable));
    public static ControlReply Ok(string? note = null) =>
        Json(note is null ? new Dictionary<string, object?> { ["ok"] = true } : new Dictionary<string, object?> { ["ok"] = true, ["note"] = note });
    public static ControlReply Bad(string message, int code = 400) => Json(new Dictionary<string, object?> { ["error"] = message }, code);
}

public sealed class ControlPort : IDisposable
{
    private const int MaxRequest = 8 << 20;
    private readonly TcpListener listener = new(IPAddress.Loopback, 0);
    private readonly CancellationTokenSource stop = new();
    private readonly Func<ControlRequest, Task<ControlReply>> handle;
    private readonly byte[] token;
    private byte[] state = "{}"u8.ToArray();

    public string ControlFile { get; }
    public string Token { get; }
    public int Port { get; }

    /// <summary>GET /state 回 <see cref="UpdateState"/> 的缓存，不经界面线程（界面卡住时照样答）；其余请求交 handle。</summary>
    public ControlPort(string privateRoot, Func<ControlRequest, Task<ControlReply>> handle)
    {
        this.handle = handle;
        Token = Convert.ToHexString(RandomNumberGenerator.GetBytes(16)).ToLowerInvariant();
        token = Encoding.ASCII.GetBytes(Token);
        PrivateJobRequest.EnsurePrivateRoot(privateRoot);
        Directory.CreateDirectory(privateRoot);
        ControlFile = Path.Combine(privateRoot, "control.json");
        listener.Start();
        Port = ((IPEndPoint)listener.LocalEndpoint).Port;
        // 先写临时文件再换名：客户端按修改时间等新文件，不能读到半截
        var temporary = ControlFile + ".tmp";
        File.WriteAllText(temporary, JsonSerializer.Serialize(new Dictionary<string, object?>
            { ["port"] = Port, ["token"] = Token, ["pid"] = Environment.ProcessId }));
        File.Move(temporary, ControlFile, overwrite: true);
        try { File.Delete(Path.Combine(privateRoot, ErrorFileName)); }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException) { }
        _ = AcceptAsync();
    }

    public const string ErrorFileName = "control.error";

    /// <summary>控制口起不来时也就没有 /state：把原因写进数据目录，s2armctl start 读它报给用户。写不上不影响启动器本身。</summary>
    public static void ReportStartFailure(string privateRoot, string why)
    {
        try { File.WriteAllText(Path.Combine(privateRoot, ErrorFileName), why); }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException) { }
    }

    public void UpdateState(byte[] json) => Volatile.Write(ref state, json);

    public void Dispose()
    {
        if (stop.IsCancellationRequested) return;
        stop.Cancel();
        listener.Stop();
        try { File.Delete(ControlFile); }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException) { }
    }

    private async Task AcceptAsync()
    {
        while (!stop.IsCancellationRequested)
        {
            TcpClient client;
            try { client = await listener.AcceptTcpClientAsync(stop.Token); }
            catch (Exception error) when (error is OperationCanceledException or SocketException or ObjectDisposedException) { return; }
            _ = ServeAsync(client);
        }
    }

    private async Task ServeAsync(TcpClient client)
    {
        using (client)
        {
            try
            {
                using var limit = CancellationTokenSource.CreateLinkedTokenSource(stop.Token);
                limit.CancelAfter(TimeSpan.FromSeconds(30));
                var stream = client.GetStream();
                var buffer = new List<byte>(4096);
                var chunk = new byte[65536];
                ControlRequest? request = null;
                while (request is null)
                {
                    var read = await stream.ReadAsync(chunk, limit.Token);
                    if (read == 0 || buffer.Count + read > MaxRequest) return;
                    buffer.AddRange(chunk.AsSpan(0, read));
                    request = ControlRequest.Parse(System.Runtime.InteropServices.CollectionsMarshal.AsSpan(buffer));
                }
                var given = Encoding.ASCII.GetBytes(request.Headers.TryGetValue("X-S2Arm-Token", out var t) ? t : "");
                ControlReply reply;
                if (!CryptographicOperations.FixedTimeEquals(given, token)) reply = ControlReply.Bad("令牌不对", 401);
                else if (request is { Method: "GET", Path: "/state" }) reply = new(200, "application/json; charset=utf-8", Volatile.Read(ref state));
                else reply = await handle(request);
                var head = Encoding.ASCII.GetBytes($"HTTP/1.1 {reply.Code} {(reply.Code == 200 ? "OK" : "ERR")}\r\nContent-Type: {reply.Type}\r\nContent-Length: {reply.Body.Length}\r\nConnection: close\r\n\r\n");
                await stream.WriteAsync(head, stop.Token);
                await stream.WriteAsync(reply.Body, stop.Token);
            }
            catch (Exception error) when (error is IOException or SocketException or OperationCanceledException or ObjectDisposedException) { }
        }
    }
}

/// <summary>作业层自己写的作业记录（pack/jobs/core.py：&lt;私有目录&gt;/jobs/&lt;任务&gt;.json）当 GET /state 的 tasks，同 Mac ControlJournal。</summary>
public static class ControlJournal
{
    private static readonly HashSet<string> Visible = new(StringComparer.Ordinal)
        { "install", "installRemove", "import", "keys", "ipa", "setup" };

    // 进程被强制结束时作业层来不及写终态：记录还是 running 而进程已不在，要按中断报，否则 s2armctl 一直等
    private static JsonElement Reconcile(JsonElement task)
    {
        if (!task.TryGetProperty("state", out var state) || state.GetString() is not ("running" or "cancelling") ||
            !task.TryGetProperty("pid", out var pid) || pid.ValueKind != JsonValueKind.Number || !pid.TryGetInt32(out var id) || Alive(id)) return task.Clone();
        var node = System.Text.Json.Nodes.JsonNode.Parse(task.GetRawText())!.AsObject();
        node["state"] = "interrupted"; node["message"] = "作业进程已不在";
        return JsonSerializer.SerializeToElement(node);
    }

    private static bool Alive(int pid)
    {
        try { using var process = System.Diagnostics.Process.GetProcessById(pid); return !process.HasExited; }
        catch (Exception error) when (error is ArgumentException or InvalidOperationException) { return false; }
    }

    public static List<JsonElement> Tasks(string privateRoot, int limit = 60)
    {
        var found = new List<(double Started, JsonElement Task)>();
        string[] files;
        try { files = Directory.GetFiles(Path.Combine(privateRoot, "jobs"), "*.json"); }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException) { return []; }
        foreach (var file in files.Where(f => !f.EndsWith(".request.json", StringComparison.OrdinalIgnoreCase)))
        {
            try
            {
                using var stream = new FileStream(file, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);
                using var document = JsonDocument.Parse(stream);
                var root = document.RootElement;
                if (root.ValueKind != JsonValueKind.Object || !root.TryGetProperty("id", out var id) || id.ValueKind != JsonValueKind.String ||
                    !root.TryGetProperty("operation", out var operation) || operation.GetString() is not { } name || !Visible.Contains(name)) continue;
                found.Add((root.TryGetProperty("started", out var s) && s.ValueKind == JsonValueKind.Number ? s.GetDouble() : 0, Reconcile(root)));
            }
            catch (Exception error) when (error is IOException or UnauthorizedAccessException or JsonException) { }   // 作业正在改写这份记录：下一秒再读
        }
        return found.OrderBy(t => t.Started).Select(t => t.Task).TakeLast(limit).ToList();
    }
}
