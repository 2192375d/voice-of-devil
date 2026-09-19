using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;

public sealed class GameHttpTransport : IDisposable
{
    private readonly HttpListener listener = new();
    private readonly CancellationTokenSource stopping = new();
    private readonly HashSet<string> origins;
    private readonly GameApiProtocol protocol;
    private readonly GameRequestInbox inbox;
    private readonly Action<string> log;
    private Timer expiryTimer;
    private const int MaxBodyBytes = 65536;

    public GameHttpTransport(GameRequestInbox inbox, IEnumerable<string> allowedOrigins = null, Action<string> log = null)
    {
        this.inbox = inbox;
        protocol = new GameApiProtocol(inbox);
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
        string requestId = Guid.NewGuid().ToString("N");
        try
        {
            var request = context.Request;
            GameHttpReply reply;
            string origin = request.Headers["Origin"];
            if (origin != null && !origins.Contains(origin))
                reply = GameApiProtocol.Error(403, requestId, "forbidden_origin", "Origin is not allowed.");
            else if (request.Url.AbsolutePath != "/api/v1/commands")
                reply = GameApiProtocol.Error(404, requestId, "not_found", "Unknown endpoint.");
            else if (request.HttpMethod != "POST")
            {
                context.Response.Headers["Allow"] = "POST";
                reply = GameApiProtocol.Error(405, requestId, "method_not_allowed", "Use POST.");
            }
            else if (!string.Equals(request.ContentType?.Split(';')[0].Trim(), "application/json", StringComparison.OrdinalIgnoreCase))
                reply = GameApiProtocol.Error(415, requestId, "unsupported_media_type", "Use Content-Type: application/json.");
            else if (request.ContentLength64 > MaxBodyBytes)
                reply = GameApiProtocol.Error(413, requestId, "request_too_large", "Request body exceeds 64 KiB.");
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
                    reply = GameApiProtocol.Error(413, requestId, "request_too_large", "Request body exceeds 64 KiB.");
                else
                    reply = await protocol.HandleAsync(Encoding.UTF8.GetString(bytes.ToArray()), requestId).ConfigureAwait(false);
            }
            await WriteAsync(context.Response, reply).ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
            try { await WriteAsync(context.Response, GameApiProtocol.Error(408, requestId, "request_timeout", "Request body deadline exceeded.")); } catch (Exception) { }
        }
        catch (Exception exception)
        {
            log("HTTP request failed: " + exception.GetType().Name);
            try { await WriteAsync(context.Response, GameApiProtocol.Error(500, requestId, "internal_error", "Request could not be processed.")); } catch (Exception) { }
        }
        finally
        {
            try { context.Response.Close(); } catch (Exception) { }
        }
    }

    private static async Task WriteAsync(HttpListenerResponse response, GameHttpReply reply)
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
