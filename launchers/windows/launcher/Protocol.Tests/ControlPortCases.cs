// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 S2Arm contributors
using System.Net.Sockets;
using System.Text;
using System.Text.Json;
using S2Arm.Launcher.Protocol;

// 控制口对 s2armctl 的约定：头 + Content-Length、令牌校验、control.json 的三个字段、/state 走缓存不经处理函数。
public static class ControlPortCases
{
    private static async Task<string> Ask(int port, string raw)
    {
        using var client = new TcpClient();
        await client.ConnectAsync("127.0.0.1", port);
        var stream = client.GetStream();
        await stream.WriteAsync(Encoding.UTF8.GetBytes(raw));
        using var reader = new StreamReader(stream, Encoding.UTF8);
        return await reader.ReadToEndAsync();
    }

    public static async Task Run(Action<bool, string> check)
    {
        check(ControlRequest.Parse("POST /keys HTTP/1.1\r\nContent-Length: 5\r\n\r\n{\"a\""u8) is null, "control: incomplete body waits");
        var parsed = ControlRequest.Parse("POST /games?x=1 HTTP/1.1\r\nContent-Length: 15\r\n\r\n{\"paths\":[\"p\"]}"u8);
        check(parsed is { Method: "POST", Path: "/games" } && parsed.Strings("paths")?.Single() == "p", "control: path without query, body strings");

        var root = Path.Combine(Path.GetTempPath(), "s2a-control-" + Guid.NewGuid().ToString("N"));
        var handled = 0;
        PrivateJobRequest.EnsurePrivateRoot(root);
        Directory.CreateDirectory(root);
        ControlPort.ReportStartFailure(root, "port busy");
        var errorFile = Path.Combine(root, ControlPort.ErrorFileName);
        check(File.ReadAllText(errorFile) == "port busy", "control: start failure reason reaches the data root");
        using (var port = new ControlPort(root, r => { handled++; return Task.FromResult(ControlReply.Ok()); }))
        {
            check(!File.Exists(errorFile), "control: successful start clears the old failure");
            using var info = JsonDocument.Parse(File.ReadAllText(port.ControlFile));
            check(info.RootElement.GetProperty("port").GetInt32() == port.Port && info.RootElement.GetProperty("token").GetString() == port.Token &&
                  info.RootElement.GetProperty("pid").GetInt32() == Environment.ProcessId, "control: control.json port, token, pid");
            check((await Ask(port.Port, "POST /refresh HTTP/1.1\r\nX-S2Arm-Token: wrong\r\n\r\n")).StartsWith("HTTP/1.1 401") && handled == 0, "control: wrong token refused before the handler");
            port.UpdateState("{\"cached\":true}"u8.ToArray());
            var state = await Ask(port.Port, $"GET /state HTTP/1.1\r\nX-S2Arm-Token: {port.Token}\r\n\r\n");
            check(state.StartsWith("HTTP/1.1 200") && state.EndsWith("{\"cached\":true}") && handled == 0, "control: /state served from cache");
            check((await Ask(port.Port, $"POST /refresh HTTP/1.1\r\nX-S2Arm-Token: {port.Token}\r\n\r\n")).StartsWith("HTTP/1.1 200") && handled == 1, "control: command reaches the handler");
            var file = port.ControlFile;
            port.Dispose();
            check(!File.Exists(file), "control: stop removes control.json");
        }
        // 作业记录：看不见的作业不进；running 且进程还在照报；running 但进程没了按中断报
        var jobs = Path.Combine(root, "jobs");
        Directory.CreateDirectory(jobs);
        File.WriteAllText(Path.Combine(jobs, "a.json"), "{\"id\":\"a\",\"operation\":\"catalog\",\"state\":\"completed\",\"started\":1}");
        File.WriteAllText(Path.Combine(jobs, "b.json"), $"{{\"id\":\"b\",\"operation\":\"ipa\",\"state\":\"running\",\"started\":2,\"pid\":{Environment.ProcessId}}}");
        File.WriteAllText(Path.Combine(jobs, "c.json"), "{\"id\":\"c\",\"operation\":\"ipa\",\"state\":\"running\",\"started\":3,\"pid\":2147483646}");
        File.WriteAllText(Path.Combine(jobs, "c.request.json"), "{\"id\":\"x\",\"operation\":\"ipa\"}");
        var tasks = ControlJournal.Tasks(root);
        check(tasks.Count == 2 && tasks[0].GetProperty("id").GetString() == "b" && tasks[0].GetProperty("state").GetString() == "running", "journal: live job stays running, hidden and request files skipped");
        check(tasks[1].GetProperty("state").GetString() == "interrupted", "journal: running job with a dead process reads interrupted");
        Directory.Delete(root, true);
    }
}
