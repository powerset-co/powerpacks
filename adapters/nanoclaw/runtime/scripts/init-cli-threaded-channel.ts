/**
 * Idempotently wire cli-threaded/local to the same agent group as cli/local.
 */
import path from 'path';

import { DATA_DIR } from '../src/config.js';
import { initDb, getDb } from '../src/db/connection.js';
import {
  createMessagingGroup,
  createMessagingGroupAgent,
  getMessagingGroupAgentByPair,
  getMessagingGroupByPlatform,
  updateMessagingGroupAgent,
} from '../src/db/messaging-groups.js';
import { runMigrations } from '../src/db/migrations/index.js';
import type { MessagingGroup } from '../src/types.js';

const CHANNEL = 'cli-threaded';
const PLATFORM_ID = 'local';

function generateId(prefix: string): string {
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

function findCliAgentGroupId(): string | null {
  const row = getDb()
    .prepare(
      `
      SELECT mga.agent_group_id
        FROM messaging_group_agents mga
        JOIN messaging_groups mg ON mg.id = mga.messaging_group_id
       WHERE mg.channel_type = 'cli'
         AND mg.platform_id = 'local'
       ORDER BY mga.priority DESC, mga.created_at DESC
       LIMIT 1
      `,
    )
    .get() as { agent_group_id: string } | undefined;
  return row?.agent_group_id ?? null;
}

async function main(): Promise<void> {
  const db = initDb(path.join(DATA_DIR, 'v2.db'));
  runMigrations(db);

  const agentGroupId = findCliAgentGroupId();
  if (!agentGroupId) {
    console.error('No cli/local agent wiring found. Run NanoClaw setup or scripts/init-cli-agent.ts first.');
    process.exit(2);
  }

  const now = new Date().toISOString();
  let mg: MessagingGroup | undefined = getMessagingGroupByPlatform(CHANNEL, PLATFORM_ID);
  if (!mg) {
    mg = {
      id: generateId('mg'),
      channel_type: CHANNEL,
      platform_id: PLATFORM_ID,
      name: 'Threaded CLI',
      is_group: 1,
      unknown_sender_policy: 'public',
      created_at: now,
    };
    createMessagingGroup(mg);
    console.log(`Created ${CHANNEL}/${PLATFORM_ID} messaging group: ${mg.id}`);
  } else {
    console.log(`Reusing ${CHANNEL}/${PLATFORM_ID} messaging group: ${mg.id}`);
  }

  const existing = getMessagingGroupAgentByPair(mg.id, agentGroupId);
  if (!existing) {
    createMessagingGroupAgent({
      id: generateId('mga'),
      messaging_group_id: mg.id,
      agent_group_id: agentGroupId,
      engage_mode: 'pattern',
      engage_pattern: '.',
      sender_scope: 'all',
      ignored_message_policy: 'drop',
      session_mode: 'per-thread',
      priority: 0,
      created_at: now,
    });
    console.log(`Wired ${CHANNEL}: ${mg.id} -> ${agentGroupId} (per-thread)`);
  } else if (existing.session_mode !== 'per-thread') {
    updateMessagingGroupAgent(existing.id, { session_mode: 'per-thread' });
    console.log(`Updated ${CHANNEL} wiring ${existing.id} to per-thread`);
  } else {
    console.log(`Wiring already exists: ${existing.id} (per-thread)`);
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
