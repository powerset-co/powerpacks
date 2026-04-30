/**
 * Chat with NanoClaw through the threaded CLI channel.
 *
 * Usage:
 *   pnpm exec tsx scripts/chat-threaded.ts --thread <thread-id> <message...>
 */
import net from 'net';
import path from 'path';

import { DATA_DIR } from '../src/config.js';

const SILENCE_MS = Number(process.env.POWERPACKS_CHAT_SILENCE_MS || '2000');
const TOTAL_TIMEOUT_MS = Number(process.env.POWERPACKS_CHAT_TOTAL_TIMEOUT_MS || '300000');
const SEND_ONLY = process.env.POWERPACKS_CHAT_SEND_ONLY === '1';

function socketPath(): string {
  return path.join(DATA_DIR, 'cli-threaded.sock');
}

function parseArgs(argv: string[]): { threadId: string; text: string } {
  let threadId = '';
  const words: string[] = [];
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i]!;
    if (arg === '--thread' || arg === '--thread-id') {
      threadId = argv[i + 1] || '';
      i++;
    } else if (arg.startsWith('--thread=')) {
      threadId = arg.slice('--thread='.length);
    } else if (arg.startsWith('--thread-id=')) {
      threadId = arg.slice('--thread-id='.length);
    } else {
      words.push(arg);
    }
  }
  if (!threadId.trim()) {
    console.error('usage: pnpm exec tsx scripts/chat-threaded.ts --thread <thread-id> <message...>');
    process.exit(1);
  }
  if (words.length === 0) {
    console.error('usage: pnpm exec tsx scripts/chat-threaded.ts --thread <thread-id> <message...>');
    process.exit(1);
  }
  return { threadId: threadId.trim(), text: words.join(' ') };
}

function main(): void {
  const { threadId, text } = parseArgs(process.argv.slice(2));
  const socket = net.connect(socketPath());

  socket.on('error', (err) => {
    const e = err as NodeJS.ErrnoException;
    if (e.code === 'ENOENT' || e.code === 'ECONNREFUSED') {
      console.error(`Threaded CLI socket not reachable at ${socketPath()}.`);
      console.error('Start or restart the NanoClaw service after installing cli-threaded.');
    } else {
      console.error('Threaded CLI socket error:', err);
    }
    process.exit(2);
  });

  let firstReplySeen = false;
  let silenceTimer: NodeJS.Timeout | null = null;
  let hardTimer: NodeJS.Timeout | null = null;

  function scheduleExit(): void {
    if (silenceTimer) clearTimeout(silenceTimer);
    silenceTimer = setTimeout(() => {
      socket.end();
      process.exit(0);
    }, SILENCE_MS);
  }

  socket.on('connect', () => {
    socket.write(JSON.stringify({ text, threadId }) + '\n', () => {
      if (SEND_ONLY) {
        socket.end();
        process.exit(0);
      }
    });
    hardTimer = setTimeout(() => {
      if (!firstReplySeen) {
        console.error(`timeout: no reply in ${TOTAL_TIMEOUT_MS}ms`);
        socket.end();
        process.exit(3);
      }
    }, TOTAL_TIMEOUT_MS);
  });

  let buffer = '';
  socket.on('data', (chunk) => {
    buffer += chunk.toString('utf8');
    let idx: number;
    while ((idx = buffer.indexOf('\n')) >= 0) {
      const line = buffer.slice(0, idx).trim();
      buffer = buffer.slice(idx + 1);
      if (!line) continue;
      try {
        const msg = JSON.parse(line);
        if (typeof msg.text === 'string') {
          process.stdout.write(msg.text + '\n');
          firstReplySeen = true;
          if (hardTimer) {
            clearTimeout(hardTimer);
            hardTimer = null;
          }
          scheduleExit();
        }
      } catch {
        // Ignore non-JSON lines.
      }
    }
  });

  socket.on('close', () => {
    if (silenceTimer) clearTimeout(silenceTimer);
    if (hardTimer) clearTimeout(hardTimer);
    process.exit(firstReplySeen ? 0 : 3);
  });
}

main();
