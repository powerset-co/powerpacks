"""Bounded person–topic links from current saved claims, without evidence text."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from enum import StrEnum

from packs.memory.directory import Directory, Origin, Selector
from packs.memory.store import Store

MAX_NODES = 1000
EDGES_PER_NODE = 4
# These are saved field categories, not shared subjects. Original human tags retain their labels.
_FIELD_TOPICS = (
    'title', 'school', 'field_of_study', 'location', 'relationship', 'employment', 'event', 'shared context',
    'human note', 'headline', 'summary', 'location_str', 'schoolname', 'degree', 'fieldofstudy', 'education',
    'field of study', 'email identifier', 'email address', 'phone identifier', 'employer', 'employer listing',
    'name', 'alias', 'canonical name', 'education institution', 'relationship to owner',
)


class NodeKind(StrEnum):
    PERSON = 'person'
    TOPIC = 'topic'


@dataclass(frozen=True)
class Node:
    id: str
    label: str
    kind: NodeKind


@dataclass(frozen=True)
class Edge:
    source: str
    target: str


@dataclass(frozen=True)
class Graph:
    corpus: str
    nodes: tuple[Node, ...]
    edges: tuple[Edge, ...]
    selected_ids: tuple[str, ...]
    total_people: int
    total_topics: int
    total_edges: int
    truncated: bool


def graph(store: Store, *, limit: int = MAX_NODES, parent: str = '', topic: str = '', selector: Selector | None = None) -> Graph:
    if not 2 <= limit <= MAX_NODES:
        raise ValueError('graph limit outside 2..1000')
    directory = Directory(store)
    prefix = directory.corpus + '#'
    names = {row['parent'][len(prefix):]: row['name'] for row in store.conn.execute(
        'SELECT parent,min(name) name FROM identities WHERE present=1 GROUP BY parent ORDER BY parent')}
    links = {(row['parent'][len(prefix):], row['topic']) for row in store.conn.execute(
        'SELECT DISTINCT c.parent,coalesce(a.topic,c.topic) topic FROM active_claims c '
        'LEFT JOIN aliases a ON a.alias=c.topic AND a.confirmed=1 '
        "WHERE c.kind IN ('mention','tag') AND (coalesce(a.topic,c.topic) NOT IN (" + ','.join('?' for _ in _FIELD_TOPICS) + ') '
        "OR (c.source_key LIKE 'tags:%' AND c.chunk_id IS NULL AND c.human=0))", _FIELD_TOPICS)}
    topics = Counter(topic for _, topic in links)
    selected = {parent} & names.keys()
    selected_topic = store.topic(topic) if topic else ''
    if selected_topic:
        selected.update(person for person, linked_topic in links if linked_topic == selected_topic)
    if selector is not None:
        sql, params = directory.matching_sql(selector)
        selected.update(row[0][len(prefix):] for row in store.conn.execute('SELECT DISTINCT parent FROM (' + sql + ')', params))
        if not topic and selector.origin in (Origin.TOPIC, Origin.HUMAN_TAG):
            selected_topic = store.topic(selector.value)

    people_limit = min(len(names), max(1, limit * 3 // 4))
    people = sorted(names, key=lambda person: (person not in selected, person))[:people_limit]
    visible_people = set(people)
    related_topics = {topic for person, topic in links if person in selected}
    connected_topics = {topic for person, topic in links if person in visible_people}
    visible_topics = sorted(connected_topics, key=lambda topic: (topic != selected_topic, topic not in related_topics, -topics[topic], topic))[:limit - len(people)]
    visible_topic_set = set(visible_topics)
    nodes = tuple(Node(person, names[person], NodeKind.PERSON) for person in people) + tuple(
        Node('topic:' + topic, topic, NodeKind.TOPIC) for topic in visible_topics)
    visible_links = sorted(((person, topic) for person, topic in links if person in visible_people and topic in visible_topic_set),
                           key=lambda link: (link[0] not in selected, link[1] != selected_topic, -topics[link[1]], link))
    edges = tuple(Edge(person, 'topic:' + topic) for person, topic in visible_links[:limit * EDGES_PER_NODE])
    selected_ids = tuple(person for person in people if person in selected)
    if selected_topic in visible_topic_set:
        selected_ids += ('topic:' + selected_topic,)
    return Graph(directory.corpus, nodes, edges, selected_ids, len(names), len(topics), len(links),
                 len(nodes) < len(names) + len(topics) or len(edges) < len(links))
