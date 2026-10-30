SELECT id, title, visibility, status, created_at
FROM public.documents
WHERE id  IN (
    '086f4edd-2e55-4b99-911a-7e77976c9c9b',
    'e7aada58-4b9a-49e8-8a4a-4d89aa6328fe'
)
ORDER BY created_at;