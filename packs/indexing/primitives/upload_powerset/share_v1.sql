-- The isolated shared network the People upload writes, for now: empty copies of the three
-- tables the upload writes, beside powerset_v2 (whose users table it still reads).
-- Created: 2026-10-08. Applied once with psql against the hosted database.
CREATE SCHEMA IF NOT EXISTS powerset_share_v1;
CREATE TABLE IF NOT EXISTS powerset_share_v1.persons (LIKE powerset_v2.persons INCLUDING ALL);
CREATE TABLE IF NOT EXISTS powerset_share_v1.operator_person_sources
    (LIKE powerset_v2.operator_person_sources INCLUDING ALL);
CREATE TABLE IF NOT EXISTS powerset_share_v1.contact_tags (LIKE powerset_v2.contact_tags INCLUDING ALL);
