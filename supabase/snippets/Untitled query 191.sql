SELECT
    p.id,
    p.full_name,
    p.role,
    u.email
FROM public.profiles p
LEFT JOIN auth.users u
    ON u.id = p.id
WHERE p.role = 'customer'
ORDER BY p.full_name;