-- ============================================================
-- ShopPilot AI
-- Phase — Row Level Security
-- ============================================================

-- ============================================================
-- 1. Private schema for trusted RLS helper functions
-- ============================================================

create schema if not exists private;

revoke all on schema private from public;

grant usage on schema private to authenticated;


-- ============================================================
-- 2. Trusted role helper
--
-- IMPORTANT:
-- - Uses auth.uid() as the authenticated identity.
-- - Reads role from public.profiles.
-- - Does not accept user_id or role from the client.
-- - SECURITY DEFINER avoids RLS recursion when reading profiles.
-- ============================================================

create or replace function private.is_support_agent_or_admin()
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
    select exists (
        select 1
        from public.profiles
        where id = (select auth.uid())
          and role::text in ('support_agent', 'admin')
    );
$$;

revoke execute
on function private.is_support_agent_or_admin()
from public;

grant execute
on function private.is_support_agent_or_admin()
to authenticated;


-- ============================================================
-- 3. Enable RLS
-- ============================================================

alter table public.orders enable row level security;
alter table public.tickets enable row level security;
alter table public.conversations enable row level security;
alter table public.documents enable row level security;

-- Companion tables containing protected child data.
-- These are necessary so users cannot bypass the parent-table
-- policies by querying child rows directly.

alter table public.order_items enable row level security;
alter table public.messages enable row level security;
alter table public.document_chunks enable row level security;


-- ============================================================
-- 4. Grants
--
-- RLS decides WHICH rows are visible.
-- Grants decide whether the role can perform SELECT at all.
-- ============================================================

grant select on table public.orders
to authenticated;

grant select on table public.tickets
to authenticated;

grant select on table public.conversations
to authenticated;

grant select on table public.documents
to authenticated;

grant select on table public.order_items
to authenticated;

grant select on table public.messages
to authenticated;

grant select on table public.document_chunks
to authenticated;


-- ============================================================
-- 5. ORDERS
--
-- customer       -> own orders only
-- support_agent  -> all orders
-- admin          -> all orders
-- ============================================================

drop policy if exists "orders_select_customer_or_staff"
on public.orders;

create policy "orders_select_customer_or_staff"
on public.orders
for select
to authenticated
using (
    customer_id = (select auth.uid())
    or
    (select private.is_support_agent_or_admin())
);


-- ============================================================
-- 6. TICKETS
--
-- customer       -> own tickets only
-- support_agent  -> all tickets
-- admin          -> all tickets
-- ============================================================

drop policy if exists "tickets_select_customer_or_staff"
on public.tickets;

create policy "tickets_select_customer_or_staff"
on public.tickets
for select
to authenticated
using (
    customer_id = (select auth.uid())
    or
    (select private.is_support_agent_or_admin())
);


-- ============================================================
-- 7. CONVERSATIONS
--
-- customer       -> own conversations only
-- support_agent  -> no unrestricted access
-- admin          -> no unrestricted access
--
-- Employee conversation access will be introduced later
-- through explicit escalation/takeover workflows.
-- ============================================================

drop policy if exists "conversations_select_owner"
on public.conversations;

create policy "conversations_select_owner"
on public.conversations
for select
to authenticated
using (
    user_id = (select auth.uid())
);


-- ============================================================
-- 8. DOCUMENTS
--
-- public    -> all authenticated users
-- internal  -> support_agent/admin only
-- ============================================================

drop policy if exists "documents_select_by_visibility"
on public.documents;

create policy "documents_select_by_visibility"
on public.documents
for select
to authenticated
using (
    visibility = 'public'
    or
    (select private.is_support_agent_or_admin())
);


-- ============================================================
-- 9. ORDER ITEMS
--
-- Companion policy for orders.
--
-- customer       -> items belonging to own orders
-- support_agent  -> items belonging to any order
-- admin          -> items belonging to any order
-- ============================================================

drop policy if exists "order_items_select_via_order_access"
on public.order_items;

create policy "order_items_select_via_order_access"
on public.order_items
for select
to authenticated
using (
    exists (
        select 1
        from public.orders as o
        where o.id = order_items.order_id
          and (
              o.customer_id = (select auth.uid())
              or
              (select private.is_support_agent_or_admin())
          )
    )
);


-- ============================================================
-- 10. MESSAGES
--
-- Messages belong to conversations.
--
-- customer -> messages belonging to own conversation
-- support_agent/admin -> no unrestricted conversation access
-- ============================================================

drop policy if exists "messages_select_via_conversation_access"
on public.messages;

create policy "messages_select_via_conversation_access"
on public.messages
for select
to authenticated
using (
    exists (
        select 1
        from public.conversations as c
        where c.id = messages.conversation_id
          and c.user_id = (select auth.uid())
    )
);


-- ============================================================
-- 11. DOCUMENT CHUNKS
--
-- Chunks inherit access from their parent document.
--
-- public document    -> all authenticated users
-- internal document  -> support_agent/admin only
-- ============================================================

drop policy if exists "document_chunks_select_via_document_access"
on public.document_chunks;

create policy "document_chunks_select_via_document_access"
on public.document_chunks
for select
to authenticated
using (
    exists (
        select 1
        from public.documents as d
        where d.id = document_chunks.document_id
          and (
              d.visibility = 'public'
              or
              (select private.is_support_agent_or_admin())
          )
    )
);


-- ============================================================
-- 12. No INSERT / UPDATE / DELETE policies yet
--
-- This phase only implements the required SELECT visibility.
-- Future write policies belong to the phases that introduce
-- those operations.
-- ============================================================