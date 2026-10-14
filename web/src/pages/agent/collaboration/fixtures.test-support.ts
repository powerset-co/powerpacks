import type { Collaboration, Member, SharedMessage } from "@/lib/api/collaboration"

// Synthetic fixtures for component tests.
const members: [Member, Member, Member] = [
  { operator_id: "arthur", name: "Arthur" },
  { operator_id: "jordan", name: "Jordan Bravo" },
  { operator_id: "casey", name: "Casey Example" },
]

export function demoMessage(text: string, author: Member = members[0]): SharedMessage {
  return {
    id: crypto.randomUUID(),
    author_id: author.operator_id,
    author_name: author.name,
    text,
    created_at: new Date().toISOString(),
  }
}

export function demoCollaboration(): Collaboration {
  return {
    me: members[0],
    sets: [
      { set_id: "sail-demo", name: "Sail · demo", members },
      { set_id: "founders-demo", name: "Founders · demo", members: members.slice(0, 2) },
    ],
    conversations: [
      {
        id: "backend",
        search_id: "backend-demo",
        set_id: "sail-demo",
        title: "Backend engineers · New York",
        kind: "search",
        query: "Backend engineers in New York with hands-on Kubernetes performance experience.",
        messages: [
          demoMessage(
            "Looking for someone who has actually tuned Kubernetes in production. A generic infrastructure title isn't enough.",
          ),
          demoMessage(
            "I have a few people in mind. Let's check the professional context alongside their profiles.",
            members[1],
          ),
          {
            ...demoMessage("How well do you know Avery Sample? Could their Kubernetes work be relevant?"),
            request_id: "sample-answer",
            recipient_id: "jordan",
            status: "answered",
            answer:
              "**Avery Sample is worth a closer look.** The demo dossier describes a professional discussion about Kubernetes performance. Their public profile only says software engineer.\n\nThere is direct contact, but no evidence that Jordan has agreed to an introduction. Current location is still unconfirmed.",
          },
        ],
      },
      {
        id: "introductions",
        search_id: "infra-demo",
        set_id: "sail-demo",
        title: "Infrastructure leads",
        kind: "search",
        messages: [demoMessage("Find infrastructure leads with experience scaling a team.", members[2])],
      },
      {
        id: "founders",
        search_id: "founders-demo",
        set_id: "founders-demo",
        title: "Who is building developer tools?",
        kind: "search",
        messages: [demoMessage("A separate conversation, visible only to the Founders demo set.")],
      },
    ],
  }
}
