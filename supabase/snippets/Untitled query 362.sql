SELECT
    p.full_name,
    p.role,
    o.id AS order_id,
    o.status,
    o.total
FROM public.orders o
JOIN public.profiles p ON p.id = o.customer_id
ORDER BY p.full_name, o.id;