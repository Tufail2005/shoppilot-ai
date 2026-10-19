ALTER TABLE public.tickets
ADD COLUMN IF NOT EXISTS assigned_agent_id UUID
REFERENCES auth.users(id)
ON DELETE SET NULL;