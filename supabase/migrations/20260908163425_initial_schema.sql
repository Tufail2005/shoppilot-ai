-- =========================================================
-- Extensions
-- =========================================================

create extension if not exists vector with schema extensions;


-- =========================================================
-- ENUMS
-- =========================================================

create type public.user_role as enum (
    'customer',
    'support_agent',
    'admin'
);

create type public.order_status as enum (
    'pending',
    'confirmed',
    'processing',
    'shipped',
    'delivered',
    'cancelled',
    'refunded'
);

create type public.ticket_status as enum (
    'open',
    'in_progress',
    'waiting_customer',
    'resolved',
    'closed'
);

create type public.ticket_priority as enum (
    'low',
    'medium',
    'high',
    'urgent'
);

create type public.document_visibility as enum (
    'public',
    'internal'
);

create type public.document_status as enum (
    'processing',
    'ready',
    'failed'
);

create type public.tool_run_status as enum (
    'running',
    'success',
    'failed'
);

create type public.escalation_status as enum (
    'pending',
    'approved',
    'rejected',
    'resolved'
);


-- =========================================================
-- PROFILES
-- =========================================================

create table public.profiles (
    id uuid primary key references auth.users(id) on delete cascade,
    role public.user_role not null default 'customer',
    full_name text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);


-- =========================================================
-- PRODUCTS
-- =========================================================

create table public.products (
    id uuid primary key default gen_random_uuid(),
    sku text not null unique,
    name text not null,
    description text,
    price numeric(12, 2) not null check (price >= 0),
    stock integer not null default 0 check (stock >= 0),
    category text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);


-- =========================================================
-- ORDERS
-- =========================================================

create table public.orders (
    id uuid primary key default gen_random_uuid(),
    customer_id uuid not null references public.profiles(id) on delete restrict,
    status public.order_status not null default 'pending',
    total numeric(12, 2) not null default 0 check (total >= 0),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);


create table public.order_items (
    id uuid primary key default gen_random_uuid(),
    order_id uuid not null references public.orders(id) on delete cascade,
    product_id uuid not null references public.products(id) on delete restrict,
    quantity integer not null check (quantity > 0),
    price numeric(12, 2) not null check (price >= 0),
    created_at timestamptz not null default now()
);


-- =========================================================
-- TICKETS
-- =========================================================

create table public.tickets (
    id uuid primary key default gen_random_uuid(),
    customer_id uuid not null references public.profiles(id) on delete restrict,
    order_id uuid references public.orders(id) on delete set null,
    subject text not null,
    description text,
    status public.ticket_status not null default 'open',
    priority public.ticket_priority not null default 'medium',
    assigned_to uuid references public.profiles(id) on delete set null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);


-- =========================================================
-- DOCUMENTS
-- =========================================================

create table public.documents (
    id uuid primary key default gen_random_uuid(),
    title text not null,
    storage_path text not null unique,
    visibility public.document_visibility not null default 'public',
    status public.document_status not null default 'processing',
    version integer not null default 1 check (version > 0),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);


-- =========================================================
-- DOCUMENT CHUNKS
-- =========================================================

create table public.document_chunks (
    id uuid primary key default gen_random_uuid(),
    document_id uuid not null references public.documents(id) on delete cascade,
    content text not null,
    embedding extensions.vector(768),
    metadata jsonb not null default '{}'::jsonb,
    chunk_index integer not null check (chunk_index >= 0),
    created_at timestamptz not null default now(),

    unique (document_id, chunk_index)
);


-- =========================================================
-- CONVERSATIONS
-- =========================================================

create table public.conversations (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references public.profiles(id) on delete cascade,
    title text,
    summary text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);


-- =========================================================
-- MESSAGES
-- =========================================================

create table public.messages (
    id uuid primary key default gen_random_uuid(),
    conversation_id uuid not null references public.conversations(id) on delete cascade,
    role text not null check (
        role in ('system', 'user', 'assistant', 'tool')
    ),
    content text not null,
    citations jsonb not null default '[]'::jsonb,
    created_at timestamptz not null default now()
);


-- =========================================================
-- TOOL RUNS
-- =========================================================

create table public.tool_runs (
    id uuid primary key default gen_random_uuid(),
    conversation_id uuid not null references public.conversations(id) on delete cascade,
    tool_name text not null,
    params jsonb not null default '{}'::jsonb,
    result jsonb,
    status public.tool_run_status not null default 'running',
    latency_ms integer check (latency_ms >= 0),
    error_message text,
    created_at timestamptz not null default now(),
    completed_at timestamptz
);


-- =========================================================
-- ESCALATIONS
-- =========================================================

create table public.escalations (
    id uuid primary key default gen_random_uuid(),
    conversation_id uuid not null references public.conversations(id) on delete cascade,
    type text not null,
    proposed_action jsonb not null default '{}'::jsonb,
    status public.escalation_status not null default 'pending',
    resolved_by uuid references public.profiles(id) on delete set null,
    created_at timestamptz not null default now(),
    resolved_at timestamptz
);


-- =========================================================
-- FEEDBACK
-- =========================================================

create table public.feedback (
    id uuid primary key default gen_random_uuid(),
    message_id uuid not null references public.messages(id) on delete cascade,
    rating smallint not null check (rating in (-1, 1)),
    created_at timestamptz not null default now(),

    unique (message_id)
);


-- =========================================================
-- AUDIT LOGS
-- =========================================================

create table public.audit_logs (
    id uuid primary key default gen_random_uuid(),
    actor_id uuid references public.profiles(id) on delete set null,
    action text not null,
    resource text not null,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now()
);


-- =========================================================
-- INDEXES
-- =========================================================

create index idx_orders_customer_id
    on public.orders(customer_id);

create index idx_orders_status
    on public.orders(status);

create index idx_order_items_order_id
    on public.order_items(order_id);

create index idx_order_items_product_id
    on public.order_items(product_id);

create index idx_tickets_customer_id
    on public.tickets(customer_id);

create index idx_tickets_assigned_to
    on public.tickets(assigned_to);

create index idx_tickets_status
    on public.tickets(status);

create index idx_document_chunks_document_id
    on public.document_chunks(document_id);

create index idx_conversations_user_id
    on public.conversations(user_id);

create index idx_messages_conversation_id_created_at
    on public.messages(conversation_id, created_at);

create index idx_tool_runs_conversation_id
    on public.tool_runs(conversation_id);

create index idx_escalations_conversation_id
    on public.escalations(conversation_id);

create index idx_escalations_status
    on public.escalations(status);

create index idx_feedback_message_id
    on public.feedback(message_id);

create index idx_audit_logs_actor_id
    on public.audit_logs(actor_id);

create index idx_audit_logs_created_at
    on public.audit_logs(created_at);


-- =========================================================
-- VECTOR INDEX
-- =========================================================

create index idx_document_chunks_embedding
    on public.document_chunks
    using ivfflat (embedding extensions.vector_cosine_ops)
    with (lists = 10);