
-- =========================================================
-- Create profile automatically when a new auth user signs up
-- =========================================================

create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
    insert into public.profiles (
        id,
        role,
        full_name
    )
    values (
        new.id,
        'customer'::public.user_role,
        new.raw_user_meta_data ->> 'full_name'
    );

    return new;
end;
$$;


-- =========================================================
-- Trigger
-- =========================================================

create trigger on_auth_user_created
after insert on auth.users
for each row
execute procedure public.handle_new_user();

