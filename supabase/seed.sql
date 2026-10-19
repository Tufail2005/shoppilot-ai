-- =========================================================
-- Seed placeholder users for database testing
-- These users cannot sign in.
-- =========================================================

INSERT INTO auth.users (
    id,
    email,
    raw_user_meta_data
)
VALUES
(
    '4447a78a-8b83-4982-8e1e-18f894115645',
    'admintest@gmail.com',
    '{"full_name":"Admin Test"}'
),
(
    '7a23da22-763c-4d79-a03d-70d044c44f38',
    'agenttest@gmail.com',
    '{"full_name":"Agent Test"}'
),
(
    'a8add1a9-697d-405d-9870-1b7dd5ea4e3b',
    'customertest@gmail.com',
    '{"full_name":"Customer Test"}'
),
(
    '64a8fb47-6be3-4755-9157-f05a30b1c1d0',
    'alice@gmail.com',
    '{"full_name":"Alice Johnson"}'
),
(
    'd7cc6c41-6dee-4ae0-a9a1-e1997c2ac6fe',
    'bob@gmail.com',
    '{"full_name":"Bob Smith"}'
),
(
    '0b89b138-8b5f-4d7f-b5c2-ce673679c300',
    'carol@gmail.com',
    '{"full_name":"Carol Williams"}'
),
(
    'f5b1e608-eadb-40e2-a78b-aa02c14f4854',
    'david-brown@shoppilot.invalid',
    '{"full_name":"David Brown"}'
),
(
    '6e0e2989-4b1b-4549-b2f6-bc7c3dc317b1',
    'emma@gmail.com',
    '{"full_name":"Emma Davis"}'
)
ON CONFLICT (id) DO NOTHING;

-- Ensure the profiles have the expected roles and names.
INSERT INTO public.profiles (id, role, full_name)
VALUES
(
    '4447a78a-8b83-4982-8e1e-18f894115645',
    'admin', 'Admin Test'
),
(
    '7a23da22-763c-4d79-a03d-70d044c44f38',
    'support_agent', 'Agent Test'
),
(
    'a8add1a9-697d-405d-9870-1b7dd5ea4e3b',
    'customer', 'Customer Test'
),
(
    '64a8fb47-6be3-4755-9157-f05a30b1c1d0',
    'admin', 'Alice Johnson'
),
(
    'd7cc6c41-6dee-4ae0-a9a1-e1997c2ac6fe',
    'support_agent', 'Bob Smith'
),
(
    '0b89b138-8b5f-4d7f-b5c2-ce673679c300',
    'customer', 'Carol Williams'
),
(
    'f5b1e608-eadb-40e2-a78b-aa02c14f4854',
    'customer', 'David Brown'
),
(
    '6e0e2989-4b1b-4549-b2f6-bc7c3dc317b1',
    'customer', 'Emma Davis'
)
ON CONFLICT (id) DO UPDATE SET
    role = EXCLUDED.role,
    full_name = EXCLUDED.full_name;


-- =========================================================
-- Products
-- =========================================================

insert into public.products (
    id,
    sku,
    name,
    description,
    price,
    stock,
    category
)
values
('10000000-0000-0000-0000-000000000001', 'TSHIRT-001', 'Classic Black T-Shirt', 'Cotton everyday t-shirt', 19.99, 100, 'Clothing'),
('10000000-0000-0000-0000-000000000002', 'TSHIRT-002', 'Classic White T-Shirt', 'Cotton everyday t-shirt', 19.99, 120, 'Clothing'),
('10000000-0000-0000-0000-000000000003', 'JEANS-001', 'Slim Fit Jeans', 'Blue slim-fit denim', 49.99, 70, 'Clothing'),
('10000000-0000-0000-0000-000000000004', 'HOODIE-001', 'Gray Hoodie', 'Fleece hoodie', 39.99, 50, 'Clothing'),
('10000000-0000-0000-0000-000000000005', 'SHOE-001', 'Everyday Sneakers', 'Casual sneakers', 59.99, 40, 'Footwear'),
('10000000-0000-0000-0000-000000000006', 'SHOE-002', 'Running Shoes', 'Lightweight running shoes', 79.99, 35, 'Footwear'),
('10000000-0000-0000-0000-000000000007', 'BAG-001', 'Urban Backpack', 'Water-resistant backpack', 44.99, 60, 'Accessories'),
('10000000-0000-0000-0000-000000000008', 'CAP-001', 'Classic Cap', 'Adjustable cotton cap', 14.99, 90, 'Accessories'),
('10000000-0000-0000-0000-000000000009', 'BELT-001', 'Leather Belt', 'Genuine leather belt', 29.99, 45, 'Accessories'),
('10000000-0000-0000-0000-000000000010', 'WALLET-001', 'Slim Wallet', 'Minimal leather wallet', 24.99, 55, 'Accessories'),
('10000000-0000-0000-0000-000000000011', 'JACKET-001', 'Denim Jacket', 'Classic denim jacket', 69.99, 25, 'Clothing'),
('10000000-0000-0000-0000-000000000012', 'SHORTS-001', 'Casual Shorts', 'Lightweight summer shorts', 24.99, 65, 'Clothing'),
('10000000-0000-0000-0000-000000000013', 'SOCKS-001', 'Sport Socks', 'Pack of 3', 9.99, 200, 'Clothing'),
('10000000-0000-0000-0000-000000000014', 'WATCH-001', 'Minimal Watch', 'Minimal analog watch', 89.99, 20, 'Accessories'),
('10000000-0000-0000-0000-000000000015', 'SUN-001', 'Classic Sunglasses', 'UV protected sunglasses', 34.99, 80, 'Accessories'),
('10000000-0000-0000-0000-000000000016', 'PERF-001', 'Fresh Eau de Parfum', 'Fresh everyday fragrance', 54.99, 35, 'Beauty'),
('10000000-0000-0000-0000-000000000017', 'CARE-001', 'Face Wash', 'Gentle daily face wash', 12.99, 100, 'Beauty'),
('10000000-0000-0000-0000-000000000018', 'CARE-002', 'Moisturizer', 'Daily moisturizer', 18.99, 85, 'Beauty'),
('10000000-0000-0000-0000-000000000019', 'HOME-001', 'Travel Mug', 'Insulated stainless steel mug', 22.99, 70, 'Home'),
('10000000-0000-0000-0000-000000000020', 'HOME-002', 'Desk Lamp', 'LED desk lamp', 31.99, 30, 'Home');


-- =========================================================
-- Orders
-- =========================================================

BEGIN;

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
-- =========================================================

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
