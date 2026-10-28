import { useState } from 'react'
import { signInWithGoogle, supabaseConfigured } from './supabase'

export default function Login() {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  const handleSignIn = async () => {
    setBusy(true)
    setError(null)
    const { error: signInError } = await signInWithGoogle()
    if (signInError) {
      setError(signInError.message)
      setBusy(false)
    }
    // On success the browser leaves for Google, so there is nothing else to do here.
  }

  return (
    <main className="page">
      <div className="hero">
        <h1>Turn meeting recordings into minutes and tasks</h1>
        <p>
          Sign in to upload a recording. MeetMate transcribes it, identifies who said what, and
          writes the summary, decisions and action items, then can send them to your own
          Calendar, Gmail, Notion and Slack.
        </p>
      </div>

      <div className="dropzone login-card">
        <button
          className="btn btn-primary btn-google"
          onClick={handleSignIn}
          disabled={busy || !supabaseConfigured}
        >
          {busy ? 'Opening Google…' : 'Continue with Google'}
        </button>
        <p className="dropzone-hint">
          Google will ask to add events to your calendar and send email as you. MeetMate only
          uses this for the meetings you upload, and you can disconnect at any time.
        </p>
        {!supabaseConfigured && (
          <div className="error-box" role="alert">
            <strong>Sign-in is not set up</strong>
            Add VITE_SUPABASE_URL and VITE_SUPABASE_ANON_KEY to the website settings.
          </div>
        )}
        {error && (
          <div className="error-box" role="alert">
            <strong>Could not sign in</strong>
            {error}
          </div>
        )}
      </div>
    </main>
  )
}
