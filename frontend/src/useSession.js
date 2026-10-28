import { useEffect, useState } from 'react'
import { supabase } from './supabase'
import { apiRequest } from './api'

// undefined = still checking, null = signed out, object = signed in.
export function useSession() {
  const [session, setSession] = useState(undefined)

  useEffect(() => {
    if (!supabase) {
      setSession(null)
      return undefined
    }
    supabase.auth.getSession().then(({ data }) => setSession(data.session))

    const {
      data: { subscription },
    } = supabase.auth.onAuthStateChange((_event, newSession) => {
      setSession(newSession)
      // Google only shows its refresh token once, right after sign-in. Hand it to the
      // backend (encrypted there) so calendar and email keep working later.
      if (newSession?.provider_refresh_token) {
        const token = newSession.access_token
        const refresh = newSession.provider_refresh_token
        // setTimeout: never call back into Supabase from inside this callback.
        setTimeout(() => {
          apiRequest('/connections/google', {
            method: 'PUT',
            token,
            body: { refresh_token: refresh },
          }).catch((err) => console.warn('Could not save the Google connection:', err.message))
        }, 0)
      }
    })
    return () => subscription.unsubscribe()
  }, [])

  return session
}
