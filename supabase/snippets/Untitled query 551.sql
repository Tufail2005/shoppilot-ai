
BEGIN;

-- =========================================================
-- Orders
-- Uses the current customer profile/auth user UUIDs.
-- Three orders per customer.
-- =========================================================

INSERT INTO public.orders (
    id,
    customer_id,
    status,
    total
)
VALUES
-- Customer Test
(
    '20000000-0000-0000-0000-000000000001',
    'a8add1a9-697d-405d-9870-1b7dd5ea4e3b',
    'delivered',
    64.98
),
(
    '20000000-0000-0000-0000-000000000002',
    'a8add1a9-697d-405d-9870-1b7dd5ea4e3b',
    'pending',
    24.99
),
(
    '20000000-0000-0000-0000-000000000003',
    'a8add1a9-697d-405d-9870-1b7dd5ea4e3b',
    'shipped',
    114.98
),

-- Carol Williams
(
    '20000000-0000-0000-0000-000000000004',
    '0b89b138-8b5f-4d7f-b5c2-ce673679c300',
    'processing',
    44.99
),
(
    '20000000-0000-0000-0000-000000000005',
    '0b89b138-8b5f-4d7f-b5c2-ce673679c300',
    'confirmed',
    99.98
),
(
    '20000000-0000-0000-0000-000000000006',
    '0b89b138-8b5f-4d7f-b5c2-ce673679c300',
    'cancelled',
    39.99
),

-- David Brown
(
    '20000000-0000-0000-0000-000000000007',
    'f5b1e608-eadb-40e2-a78b-aa02c14f4854',
    'delivered',
    69.98
),
(
    '20000000-0000-0000-0000-000000000008',
    'f5b1e608-eadb-40e2-a78b-aa02c14f4854',
    'shipped',
    67.98
),
(
    '20000000-0000-0000-0000-000000000009',
    'f5b1e608-eadb-40e2-a78b-aa02c14f4854',
    'pending',
    31.99
),

-- Emma Davis
(
    '20000000-0000-0000-0000-000000000010',
    '6e0e2989-4b1b-4549-b2f6-bc7c3dc317b1',
    'delivered',
    41.98
),
(
    '20000000-0000-0000-0000-000000000011',
    '6e0e2989-4b1b-4549-b2f6-bc7c3dc317b1',
    'processing',
    69.99
),
(
    '20000000-0000-0000-0000-000000000012',
    '6e0e2989-4b1b-4549-b2f6-bc7c3dc317b1',
    'confirmed',
    49.98
)

ON CONFLICT (id) DO UPDATE SET
    customer_id = EXCLUDED.customer_id,
    status = EXCLUDED.status,
    total = EXCLUDED.total;


-- =========================================================
-- Order items
-- Clear the items belonging to these fixed seed orders,
-- so rerunning this section does not duplicate line items.
-- =========================================================

DELETE FROM public.order_items
WHERE order_id IN (
    '20000000-0000-0000-0000-000000000001',
    '20000000-0000-0000-0000-000000000002',
    '20000000-0000-0000-0000-000000000003',
    '20000000-0000-0000-0000-000000000004',
    '20000000-0000-0000-0000-000000000005',
    '20000000-0000-0000-0000-000000000006',
    '20000000-0000-0000-0000-000000000007',
    '20000000-0000-0000-0000-000000000008',
    '20000000-0000-0000-0000-000000000009',
    '20000000-0000-0000-0000-000000000010',
    '20000000-0000-0000-0000-000000000011',
    '20000000-0000-0000-0000-000000000012'
);

INSERT INTO public.order_items (
    order_id,
    product_id,
    quantity,
    price
)
VALUES
-- Customer Test: delivered
(
    '20000000-0000-0000-0000-000000000001',
    '10000000-0000-0000-0000-000000000001',
    1,
    19.99
),
(
    '20000000-0000-0000-0000-000000000001',
    '10000000-0000-0000-0000-000000000007',
    1,
    44.99
),

-- Customer Test: pending
(
    '20000000-0000-0000-0000-000000000002',
    '10000000-0000-0000-0000-000000000010',
    1,
    24.99
),

-- Customer Test: shipped
(
    '20000000-0000-0000-0000-000000000003',
    '10000000-0000-0000-0000-000000000006',
    1,
    79.99
),
(
    '20000000-0000-0000-0000-000000000003',
    '10000000-0000-0000-0000-000000000015',
    1,
    34.99
),

-- Carol Williams: processing
(
    '20000000-0000-0000-0000-000000000004',
    '10000000-0000-0000-0000-000000000007',
    1,
    44.99
),

-- Carol Williams: confirmed
(
    '20000000-0000-0000-0000-000000000005',
    '10000000-0000-0000-0000-000000000003',
    2,
    49.99
),

-- Carol Williams: cancelled
(
    '20000000-0000-0000-0000-000000000006',
    '10000000-0000-0000-0000-000000000004',
    1,
    39.99
),

-- David Brown: delivered
(
    '20000000-0000-0000-0000-000000000007',
    '10000000-0000-0000-0000-000000000005',
    1,
    59.99
),
(
    '20000000-0000-0000-0000-000000000007',
    '10000000-0000-0000-0000-000000000013',
    1,
    9.99
),

-- David Brown: shipped
(
    '20000000-0000-0000-0000-000000000008',
    '10000000-0000-0000-0000-000000000016',
    1,
    54.99
),
(
    '20000000-0000-0000-0000-000000000008',
    '10000000-0000-0000-0000-000000000017',
    1,
    12.99
),

-- David Brown: pending
(
    '20000000-0000-0000-0000-000000000009',
    '10000000-0000-0000-0000-000000000020',
    1,
    31.99
),

-- Emma Davis: delivered
(
    '20000000-0000-0000-0000-000000000010',
    '10000000-0000-0000-0000-000000000019',
    1,
    22.99
),
(
    '20000000-0000-0000-0000-000000000010',
    '10000000-0000-0000-0000-000000000018',
    1,
    18.99
),

-- Emma Davis: processing
(
    '20000000-0000-0000-0000-000000000011',
    '10000000-0000-0000-0000-000000000011',
    1,
    69.99
),

-- Emma Davis: confirmed
(
    '20000000-0000-0000-0000-000000000012',
    '10000000-0000-0000-0000-000000000002',
    1,
    19.99
),
(
    '20000000-0000-0000-0000-000000000012',
    '10000000-0000-0000-0000-000000000009',
    1,
    29.99
);


-- =========================================================
-- Tickets
-- Bob Smith is the assigned support agent.
-- NULL assignments provide unassigned-ticket test cases.
-- =========================================================

INSERT INTO public.tickets (
    id,
    customer_id,
    order_id,
    subject,
    description,
    status,
    priority,
    assigned_agent_id
)
VALUES
(
    '30000000-0000-0000-0000-000000000001',
    'a8add1a9-697d-405d-9870-1b7dd5ea4e3b',
    '20000000-0000-0000-0000-000000000001',
    'Delivery confirmation',
    'Please confirm the delivery details for my order.',
    'resolved',
    'medium',
    'd7cc6c41-6dee-4ae0-a9a1-e1997c2ac6fe'
),
(
    '30000000-0000-0000-0000-000000000002',
    'a8add1a9-697d-405d-9870-1b7dd5ea4e3b',
    '20000000-0000-0000-0000-000000000002',
    'Pending order update',
    'My order is still pending. Can you provide an update?',
    'in_progress',
    'medium',
    'd7cc6c41-6dee-4ae0-a9a1-e1997c2ac6fe'
),
(
    '30000000-0000-0000-0000-000000000003',
    '0b89b138-8b5f-4d7f-b5c2-ce673679c300',
    '20000000-0000-0000-0000-000000000004',
    'Backpack delivery update',
    'I would like an update on my backpack order.',
    'open',
    'high',
    'd7cc6c41-6dee-4ae0-a9a1-e1997c2ac6fe'
),
(
    '30000000-0000-0000-0000-000000000004',
    '0b89b138-8b5f-4d7f-b5c2-ce673679c300',
    '20000000-0000-0000-0000-000000000006',
    'Refund for cancelled order',
    'Please explain the refund process for my cancelled order.',
    'open',
    'high',
    NULL
),
(
    '30000000-0000-0000-0000-000000000005',
    'f5b1e608-eadb-40e2-a78b-aa02c14f4854',
    '20000000-0000-0000-0000-000000000007',
    'Delivered order question',
    'I have a question about the items in my delivered order.',
    'closed',
    'low',
    'd7cc6c41-6dee-4ae0-a9a1-e1997c2ac6fe'
),
(
    '30000000-0000-0000-0000-000000000006',
    'f5b1e608-eadb-40e2-a78b-aa02c14f4854',
    '20000000-0000-0000-0000-000000000008',
    'Tracking information',
    'Please provide the latest tracking information.',
    'in_progress',
    'medium',
    NULL
),
(
    '30000000-0000-0000-0000-000000000007',
    '6e0e2989-4b1b-4549-b2f6-bc7c3dc317b1',
    '20000000-0000-0000-0000-000000000010',
    'Delivery confirmation',
    'I would like to verify the delivery of my order.',
    'resolved',
    'low',
    'd7cc6c41-6dee-4ae0-a9a1-e1997c2ac6fe'
),
(
    '30000000-0000-0000-0000-000000000008',
    '6e0e2989-4b1b-4549-b2f6-bc7c3dc317b1',
    '20000000-0000-0000-0000-000000000011',
    'Order processing delay',
    'My jacket order is still processing. Can you check it?',
    'open',
    'medium',
    NULL
)
ON CONFLICT (id) DO UPDATE SET
    customer_id = EXCLUDED.customer_id,
    order_id = EXCLUDED.order_id,
    subject = EXCLUDED.subject,
    description = EXCLUDED.description,
    status = EXCLUDED.status,
    priority = EXCLUDED.priority,
    assigned_agent_id = EXCLUDED.assigned_agent_id;

COMMIT;
