/**
 * Threaded CLI channel for local terminal clients.
 *
 * This is intentionally separate from NanoClaw's built-in `cli` channel.
 * It listens on data/cli-threaded.sock and maps each client-provided
 * threadId to a native NanoClaw thread/session.
 */
import fs from 'fs';
import net from 'net';
import path from 'path';

import { DATA_DIR } from '../config.js';
import { log } from '../log.js';
import type { ChannelAdapter, ChannelSetup, OutboundMessage } from './adapter.js';
import { registerChannelAdapter } from './channel-registry.js';

const PLATFORM_ID = 'local';
const DEFAULT_THREAD_ID = 'default';

function socketPath(): string {
  return path.join(DATA_DIR, 'cli-threaded.sock');
}

function normalizeThreadId(raw: unknown): string {
  if (typeof raw !== 'string') return DEFAULT_THREAD_ID;
  const trimmed = raw.trim();
  return trimmed.length > 0 ? trimmed : DEFAULT_THREAD_ID;
}

function createAdapter(): ChannelAdapter {
  let server: net.Server | null = null;
  const clients = new Map<string, net.Socket>();
  const socketThreads = new WeakMap<net.Socket, string>();

  const adapter: ChannelAdapter = {
    name: 'cli-threaded',
    channelType: 'cli-threaded',
    supportsThreads: true,

    async setup(config: ChannelSetup): Promise<void> {
      const sock = socketPath();
      try {
        fs.unlinkSync(sock);
      } catch (err) {
        const e = err as NodeJS.ErrnoException;
        if (e.code !== 'ENOENT') {
          log.warn('Failed to unlink stale threaded CLI socket', { sock, err });
        }
      }

      server = net.createServer((socket) => handleConnection(socket, config));
      await new Promise<void>((resolve, reject) => {
        server!.once('error', reject);
        server!.listen(sock, () => {
          try {
            fs.chmodSync(sock, 0o600);
          } catch (err) {
            log.warn('Failed to chmod threaded CLI socket', { sock, err });
          }
          log.info('Threaded CLI channel listening', { sock });
          resolve();
        });
      });
    },

    async teardown(): Promise<void> {
      for (const socket of clients.values()) {
        try {
          socket.end();
        } catch {
          // best effort
        }
      }
      clients.clear();
      if (server) {
        await new Promise<void>((resolve) => {
          server!.close(() => resolve());
        });
        server = null;
      }
      try {
        fs.unlinkSync(socketPath());
      } catch {
        // best effort
      }
    },

    isConnected(): boolean {
      return server !== null;
    },

    async deliver(platformId, threadId, message: OutboundMessage): Promise<string | undefined> {
      if (platformId !== PLATFORM_ID) return undefined;
      const threadKey = normalizeThreadId(threadId);
      const client = clients.get(threadKey);
      if (!client) {
        log.warn('Threaded CLI delivery skipped; no live client for thread', {
          threadId: threadKey,
          liveThreads: [...clients.keys()],
        });
        return undefined;
      }
      const text = extractText(message);
      if (text === null) return undefined;
      try {
        client.write(JSON.stringify({ text, threadId: threadKey }) + '\n');
        log.info('Threaded CLI wrote message to client', { threadId: threadKey });
      } catch (err) {
        log.warn('Failed to write to threaded CLI client', { threadId: threadKey, err });
      }
      return undefined;
    },
  };

  function handleConnection(socket: net.Socket, config: ChannelSetup): void {
    let buffer = '';
    socket.on('data', (chunk) => {
      buffer += chunk.toString('utf8');
      let idx: number;
      while ((idx = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, idx).trim();
        buffer = buffer.slice(idx + 1);
        if (!line) continue;
        void handleLine(socket, line, config);
      }
    });

    socket.on('close', () => {
      const threadId = socketThreads.get(socket);
      if (threadId && clients.get(threadId) === socket) {
        clients.delete(threadId);
        log.info('Threaded CLI client disconnected', { threadId });
      }
    });

    socket.on('error', (err) => {
      log.warn('Threaded CLI client socket error', { err });
    });
  }

  async function handleLine(socket: net.Socket, line: string, config: ChannelSetup): Promise<void> {
    let payload: { text?: unknown; threadId?: unknown; sender?: unknown; senderId?: unknown };
    try {
      payload = JSON.parse(line);
    } catch (err) {
      log.warn('Threaded CLI: ignoring non-JSON line from client', { line });
      return;
    }
    if (typeof payload.text !== 'string' || payload.text.length === 0) return;

    const threadId = normalizeThreadId(payload.threadId);
    const existing = clients.get(threadId);
    if (existing && existing !== socket) {
      try {
        existing.write(JSON.stringify({ text: `[thread ${threadId} superseded by a newer client]`, threadId }) + '\n');
        existing.end();
      } catch {
        // best effort
      }
    }
    clients.set(threadId, socket);
    socketThreads.set(socket, threadId);
    log.info('Threaded CLI client registered', { threadId, liveThreads: [...clients.keys()] });

    try {
      await config.onInbound(PLATFORM_ID, threadId, {
        id: `cli-threaded-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
        kind: 'chat',
        timestamp: new Date().toISOString(),
        isGroup: true,
        content: {
          text: payload.text,
          sender: typeof payload.sender === 'string' ? payload.sender : 'cli-threaded',
          senderId: typeof payload.senderId === 'string' ? payload.senderId : `cli-threaded:${PLATFORM_ID}`,
        },
      });
    } catch (err) {
      log.error('Threaded CLI: onInbound threw', { threadId, err });
    }
  }

  return adapter;
}

function extractText(message: OutboundMessage): string | null {
  const content = message.content as Record<string, unknown> | string | undefined;
  if (typeof content === 'string') return content;
  if (content && typeof content === 'object' && typeof content.text === 'string') {
    return content.text;
  }
  return null;
}

registerChannelAdapter('cli-threaded', { factory: createAdapter });
