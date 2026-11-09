SELECT
    name,
    bucket_id,
    created_at,
    updated_at
FROM storage.objects
WHERE bucket_id = 'documents'
ORDER BY created_at DESC;