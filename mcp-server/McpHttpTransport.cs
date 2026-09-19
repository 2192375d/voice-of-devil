using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;

public sealed class McpHttpTransport : IDisposable
{
    private readonly HttpListener listener = new();
    private readonly CancellationTokenSource stopping = new();
    private readonly HashSet<string> origins;
    private readonly McpProtocol protocol;
    private readonly McpInbox inbox;
    private readonly Action<string> log;
    private Timer expiryTimer;
    private const int MaxBodyBytes = 65536;

    public McpHttpTransport(McpInbox inbox, IEnumerable<string> allowedOrigins = null, Action<string> log = null)
    {
        this.inbox = inbox;
        protocol = new McpProtocol(inbox);
        origins = new HashSet<string>(allowedOrigins ?? Array.Empty<string>(), StringComparer.OrdinalIgnoreCase);
        this.log = log ?? (_ => { });
    }

    public void Start(string bindAddress, int port)
    {
        if (port < 1 || port > 65535) throw new ArgumentOutOfRangeException(nameof(port));
        string host = bindAddress == "0.0.0.0" ? "+" : bindAddress;
        listener.Prefixes.Add($"http://{host}:{port}/");
        listener.Start();
        expiryTimer = new Timer(_ => inbox.Expire(), null, 250, 250);
        _ = AcceptAsync();
    }

    private async Task AcceptAsync()
    {
        try
        {
            while (!stopping.IsCancellationRequested)
            {
                var context = await listener.GetContextAsync().ConfigureAwait(false);
                _ = HandleAsync(context);
            }
        }
        catch (Exception exception) when (exception is HttpListenerException or ObjectDisposedException)
        {
            if (!stopping.IsCancellationRequested) log("HTTP listener stopped: " + exception.Message);
        }
    }

    private async Task HandleAsync(HttpListenerContext context)
    {
        try
        {
            var request = context.Request;
            McpHttpReply reply;
            string origin = request.Headers["Origin"];
            if (origin != null && !origins.Contains(origin))
                reply = McpProtocol.Error(403, null, -32600, "Origin is not allowed.");
            else if (request.Url.AbsolutePath != "/mcp")
                reply = new McpHttpReply(404);
            else if (request.HttpMethod != "POST")
            {
                context.Response.Headers["Allow"] = "POST";
                reply = new McpHttpReply(405);
            }
            else if (request.ContentType?.Split(';')[0].Trim() != "application/json")
                reply = new McpHttpReply(415);
            else if (!Accepts(request.Headers["Accept"], "application/json") || !Accepts(request.Headers["Accept"], "text/event-stream"))
                reply = new McpHttpReply(406);
            else if (request.ContentLength64 > MaxBodyBytes)
                reply = new McpHttpReply(413);
            else
            {
                using var deadline = CancellationTokenSource.CreateLinkedTokenSource(stopping.Token);
                deadline.CancelAfter(TimeSpan.FromSeconds(10));
                using var bytes = new MemoryStream();
                var buffer = new byte[4096];
                int count;
                while ((count = await request.InputStream.ReadAsync(buffer, deadline.Token).ConfigureAwait(false)) > 0)
                {
                    if (bytes.Length + count > MaxBodyBytes) break;
                    bytes.Write(buffer, 0, count);
                }
                if (bytes.Length + count > MaxBodyBytes)
                    reply = new McpHttpReply(413);
                else
                    reply = await protocol.HandleAsync(Encoding.UTF8.GetString(bytes.ToArray()), request.Headers["MCP-Protocol-Version"]).ConfigureAwait(false);
            }
            await WriteAsync(context.Response, reply).ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
            try { context.Response.StatusCode = 408; } catch (ObjectDisposedException) { }
        }
        catch (Exception exception)
        {
            log("HTTP request failed: " + exception.GetType().Name);
            try { context.Response.StatusCode = 500; } catch (Exception) { }
        }
        finally
        {
            try { context.Response.Close(); } catch (Exception) { }
        }
    }

    private static bool Accepts(string header, string type)
    {
        foreach (string entry in (header ?? "").Split(','))
            if (entry.Split(';')[0].Trim().Equals(type, StringComparison.OrdinalIgnoreCase)) return true;
        return false;
    }

    private static async Task WriteAsync(HttpListenerResponse response, McpHttpReply reply)
    {
        response.StatusCode = reply.StatusCode;
        if (reply.Body == null) return;
        byte[] bytes = JsonSerializer.SerializeToUtf8Bytes(reply.Body);
        response.ContentType = "application/json; charset=utf-8";
        response.ContentLength64 = bytes.Length;
        await response.OutputStream.WriteAsync(bytes).ConfigureAwait(false);
    }

    public void Dispose()
    {
        if (stopping.IsCancellationRequested) return;
        stopping.Cancel();
        expiryTimer?.Dispose();
        inbox.Shutdown();
        listener.Close();
    }
}
