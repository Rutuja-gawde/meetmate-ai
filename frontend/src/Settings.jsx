import { useEffect, useState } from 'react'
import { apiRequest } from './api'
import { signInWithGoogle } from './supabase'

export default function Settings({ onClose }) {
  const [connections, setConnections] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState('')
  const [notion, setNotion] = useState({ token: '', database_id: '' })
  const [slackUrl, setSlackUrl] = useState('')

  const load = () =>
    apiRequest('/connections')
      .then(setConnections)
      .catch((err) => setError(err.message))

  useEffect(() => {
    load()
  }, [])

  const run = async (key, fn) => {
    setBusy(key)
    setError(null)
    try {
      await fn()
      await load()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy('')
    }
  }

  const disconnect = (provider) =>
    run(provider, () => apiRequest(`/connections/${provider}`, { method: 'DELETE' }))

  const saveNotion = () =>
    run('notion', async () => {
      await apiRequest('/connections/notion', { method: 'PUT', body: notion })
      setNotion({ token: '', database_id: '' })
    })

  const saveSlack = () =>
    run('slack', async () => {
      await apiRequest('/connections/slack', { method: 'PUT', body: { webhook_url: slackUrl } })
      setSlackUrl('')
    })

  const Status = ({ on }) => (
    <span className={`conn-status ${on ? 'on' : ''}`}>{on ? 'Connected' : 'Not connected'}</span>
  )

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label="Connections"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="panel-head">
          <h3 className="panel-title">Connections</h3>
          <button className="btn modal-close" onClick={onClose}>
            Close
          </button>
        </div>
        <p className="dropzone-hint" style={{ marginBottom: 16 }}>
          After each meeting, MeetMate sends the results to the apps you connect here, using your
          own accounts. Nothing is sent to an app you leave disconnected.
        </p>

        {error && (
          <div className="error-box" role="alert" style={{ marginBottom: 16 }}>
            {error}
          </div>
        )}
        {!connections && !error && <p className="empty-note">Loading…</p>}

        {connections && (
          <>
            <section className="conn-row">
              <div className="conn-head">
                <strong>Google Calendar and Gmail</strong>
                <Status on={connections.google} />
              </div>
              <p className="dropzone-hint">
                Adds due dates to your calendar and emails you the minutes. Connected when you sign
                in with Google. If it stopped working, sign in again to renew it.
              </p>
              <div className="conn-actions">
                <button className="btn" onClick={() => signInWithGoogle()}>
                  {connections.google ? 'Reconnect' : 'Connect'}
                </button>
                {connections.google && (
                  <button className="btn" disabled={busy === 'google'} onClick={() => disconnect('google')}>
                    Disconnect
                  </button>
                )}
              </div>
            </section>

            <section className="conn-row">
              <div className="conn-head">
                <strong>Notion</strong>
                <Status on={connections.notion} />
              </div>
              <p className="dropzone-hint">
                Create an internal integration at notion.so/profile/integrations, share your tasks
                database with it, then paste its token and the database link. The database needs
                the properties Task (title), Owner (text) and Due Date (date).
              </p>
              <div className="conn-form">
                <input
                  className="field"
                  type="password"
                  autoComplete="off"
                  placeholder="Integration token"
                  value={notion.token}
                  onChange={(e) => setNotion({ ...notion, token: e.target.value })}
                />
                <input
                  className="field"
                  type="text"
                  autoComplete="off"
                  placeholder="Database link or ID"
                  value={notion.database_id}
                  onChange={(e) => setNotion({ ...notion, database_id: e.target.value })}
                />
              </div>
              <div className="conn-actions">
                <button
                  className="btn btn-primary"
                  disabled={busy === 'notion' || !notion.token || !notion.database_id}
                  onClick={saveNotion}
                >
                  Save Notion
                </button>
                {connections.notion && (
                  <button className="btn" disabled={busy === 'notion'} onClick={() => disconnect('notion')}>
                    Disconnect
                  </button>
                )}
              </div>
            </section>

            <section className="conn-row">
              <div className="conn-head">
                <strong>Slack</strong>
                <Status on={connections.slack} />
              </div>
              <p className="dropzone-hint">
                Create an Incoming Webhook for the channel you want (api.slack.com/messaging/webhooks)
                and paste its URL.
              </p>
              <div className="conn-form">
                <input
                  className="field"
                  type="password"
                  autoComplete="off"
                  placeholder="https://hooks.slack.com/services/…"
                  value={slackUrl}
                  onChange={(e) => setSlackUrl(e.target.value)}
                />
              </div>
              <div className="conn-actions">
                <button className="btn btn-primary" disabled={busy === 'slack' || !slackUrl} onClick={saveSlack}>
                  Save Slack
                </button>
                {connections.slack && (
                  <button className="btn" disabled={busy === 'slack'} onClick={() => disconnect('slack')}>
                    Disconnect
                  </button>
                )}
              </div>
            </section>
          </>
        )}
      </div>
    </div>
  )
}
