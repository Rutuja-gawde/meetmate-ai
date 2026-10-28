import { useState, useEffect, useRef } from 'react'
import './App.css'
import { API_URL, apiRequest, authHeaders } from './api'
import { signOut } from './supabase'
import { useSession } from './useSession'
import Login from './Login'
import Settings from './Settings'
import History from './History'

const STATUS_ICONS = {
  ok: '✓',
  timed_out: '!',
  failed: '✕',
}

const STATUS_LABELS = {
  notion: 'Notion',
  slack: 'Slack',
  calendar: 'Google Calendar',
  email: 'Gmail',
}

const SYNC_STATE_LABELS = {
  ok: 'Synced',
  timed_out: 'Timed out',
  failed: 'Failed',
}

// The steps shown while a meeting is processed. "key" matches the stage the
// server reports, so the checklist follows what is really happening.
const PIPELINE_STEPS = [
  { key: 'uploading', label: 'Uploading the recording' },
  { key: 'transcribing', label: 'Transcribing and identifying speakers' },
  { key: 'summarizing', label: 'Writing the summary and tasks' },
  { key: 'syncing', label: 'Sending results to your connected apps' },
]

const ICONS = {
  upload: (
    <>
      <path d="M12 16V4" />
      <path d="m7 9 5-5 5 5" />
      <path d="M4 16v3a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-3" />
    </>
  ),
  download: (
    <>
      <path d="M12 4v12" />
      <path d="m7 11 5 5 5-5" />
      <path d="M4 20h16" />
    </>
  ),
  plus: (
    <>
      <path d="M12 5v14" />
      <path d="M5 12h14" />
    </>
  ),
  wave: (
    <>
      <path d="M4 10v4" />
      <path d="M8 6v12" />
      <path d="M12 3v18" />
      <path d="M16 8v8" />
      <path d="M20 10v4" />
    </>
  ),
  check: <path d="m5 12 5 5 9-10" />,
}

function Icon({ name, size = 20 }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {ICONS[name]}
    </svg>
  )
}

const escapeHtml = (value) =>
  String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')

// Agenda/decision items are normally strings, but small models sometimes
// return objects like { topic: "..." }. This keeps everything readable either way.
const itemToText = (item) => {
  if (typeof item === 'string') return item
  if (item && typeof item === 'object') {
    return item.title || item.topic || item.text || JSON.stringify(item)
  }
  return String(item ?? '')
}

// "Budget review: discussed Q3 numbers" -> topic + note
const splitTopic = (text) => {
  const i = text.indexOf(':')
  if (i > 0 && i <= 80) {
    return { topic: text.slice(0, i).trim(), note: text.slice(i + 1).trim() }
  }
  return { topic: '', note: text }
}

const getInitials = (name) => {
  if (!name || String(name).toLowerCase() === 'unassigned') return '?'
  return String(name)
    .trim()
    .split(/\s+/)
    .slice(0, 2)
    .map((w) => w[0])
    .join('')
    .toUpperCase()
}

const formatDue = (task) => {
  const raw = task.due_date || task.due_date_raw
  if (!raw || String(raw).toLowerCase() === 'unspecified') return null
  if (/^\d{4}-\d{2}-\d{2}$/.test(raw)) {
    const d = new Date(`${raw}T00:00:00`)
    if (!Number.isNaN(d.getTime())) {
      return d.toLocaleDateString(undefined, {
        weekday: 'short',
        day: 'numeric',
        month: 'short',
        year: 'numeric',
      })
    }
  }
  return String(raw)
}

// "[Shruti Gharat] hello there" -> { speaker: "Shruti Gharat", text: "hello there" }
const parseTranscript = (text) =>
  String(text ?? '')
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      const match = line.match(/^\[([^\]]+)\]\s*(.*)$/)
      return match ? { speaker: match[1], text: match[2] } : { speaker: '', text: line }
    })

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

// Uploads with XMLHttpRequest because fetch cannot report upload progress.
async function uploadRecording(file, participants, onProgress) {
  const headers = await authHeaders()
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', `${API_URL}/jobs`)
    Object.entries(headers).forEach(([name, value]) => xhr.setRequestHeader(name, value))

    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress(Math.round((event.loaded / event.total) * 100))
    }
    xhr.onload = () => {
      let body = {}
      try {
        body = JSON.parse(xhr.responseText)
      } catch {
        // response wasn't JSON
      }
      if (xhr.status >= 200 && xhr.status < 300 && body.job_id) {
        resolve({ jobId: body.job_id })
      } else if (xhr.status === 413) {
        reject(new Error(body.detail || 'This file is too large to upload.'))
      } else {
        reject(new Error(body.detail || `Upload failed (error ${xhr.status}).`))
      }
    }
    xhr.onerror = () =>
      reject(new Error('Could not reach the server. Check your connection and try again.'))

    const formData = new FormData()
    formData.append('file', file)
    formData.append('participants', participants)
    xhr.send(formData)
  })
}

const PARTICIPANTS_KEY = 'meetmate-participants'

function readSavedParticipants() {
  try {
    return localStorage.getItem(PARTICIPANTS_KEY) || ''
  } catch {
    return ''
  }
}

function saveParticipants(value) {
  try {
    localStorage.setItem(PARTICIPANTS_KEY, value)
  } catch {
    // storage can be unavailable; the names just won't be remembered
  }
}

function Workspace({ session }) {
  const [file, setFile] = useState(null)
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)
  const [elapsedSeconds, setElapsedSeconds] = useState(0)
  const [phase, setPhase] = useState('uploading')
  const [uploadPercent, setUploadPercent] = useState(0)
  const [queuePosition, setQueuePosition] = useState(0)
  const [isDragging, setIsDragging] = useState(false)
  const [mediaUrl, setMediaUrl] = useState(null)
  const [serverConfig, setServerConfig] = useState(null)
  const [jobIntegrations, setJobIntegrations] = useState(false)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [viewedName, setViewedName] = useState(null)
  const [participants, setParticipants] = useState(readSavedParticipants)
  const fileInputRef = useRef(null)
  const runIdRef = useRef(0)

  // Ask the server what it needs (access code, size limit).
  useEffect(() => {
    let cancelled = false
    fetch(`${API_URL}/config`)
      .then((response) => {
        if (!response.ok) throw new Error('bad response')
        return response.json()
      })
      .then((config) => {
        if (!cancelled) setServerConfig(config)
      })
      .catch(() => {
        if (!cancelled) {
          setError('Cannot reach the MeetMate server right now. Please try again later.')
        }
      })
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    let timer
    if (loading) {
      setElapsedSeconds(0)
      timer = setInterval(() => setElapsedSeconds((s) => s + 1), 1000)
    }
    return () => clearInterval(timer)
  }, [loading])

  // Creates a playable URL for the uploaded recording and frees it when the
  // file changes or the page closes.
  useEffect(() => {
    if (!file) {
      setMediaUrl(null)
      return
    }
    const url = URL.createObjectURL(file)
    setMediaUrl(url)
    return () => URL.revokeObjectURL(url)
  }, [file])

  const resetFile = (f) => {
    setFile(f)
    setResult(null)
    setError(null)
  }

  const handleFileChange = (e) => {
    if (e.target.files?.[0]) resetFile(e.target.files[0])
  }

  const handleDrop = (e) => {
    e.preventDefault()
    setIsDragging(false)
    if (loading) return
    const dropped = e.dataTransfer.files?.[0]
    if (dropped) resetFile(dropped)
  }

  // Polls the server until the job finishes. Used right after an upload and also when the
  // page opens while a job is already running (closed tab, refresh).
  const followJob = async (jobId, runId) => {
    const stillCurrent = () => runIdRef.current === runId
    try {
      let failures = 0
      while (stillCurrent()) {
        await sleep(2000)
        if (!stillCurrent()) return

        let response
        try {
          response = await fetch(`${API_URL}/jobs/${jobId}`, { headers: await authHeaders() })
        } catch {
          failures += 1
          if (failures >= 5) throw new Error('Lost connection to the server. Please try again.')
          continue
        }

        if (!response.ok) {
          if (response.status >= 500) {
            failures += 1
            if (failures >= 5) throw new Error('The server is having trouble. Please try again.')
            continue
          }
          const body = await response.json().catch(() => ({}))
          throw new Error(body.detail || `Server error: ${response.status}`)
        }

        failures = 0
        const data = await response.json()
        setQueuePosition(data.queue_position || 0)

        if (data.status === 'queued') {
          setPhase('queued')
        } else if (data.status === 'running') {
          setPhase(data.stage || 'transcribing')
        } else if (data.status === 'done') {
          setResult(data.result)
          setLoading(false)
          return
        } else if (data.status === 'failed') {
          throw new Error(data.error || 'Processing failed. Please try again.')
        }
      }
    } catch (err) {
      if (stillCurrent()) {
        setError(err.message)
        setLoading(false)
      }
    }
  }

  // If a recording is still being processed (the tab was closed or refreshed), pick it up again.
  useEffect(() => {
    let cancelled = false
    apiRequest('/jobs/active')
      .then((active) => {
        if (cancelled || !active?.job_id) return
        const runId = ++runIdRef.current
        setViewedName(active.filename)
        setError(null)
        setLoading(true)
        setPhase(active.status === 'queued' ? 'queued' : active.stage || 'transcribing')
        apiRequest('/connections')
          .then((connected) => {
            if (runIdRef.current === runId) setJobIntegrations(Object.values(connected).some(Boolean))
          })
          .catch(() => {})
        followJob(active.job_id, runId)
      })
      .catch(() => {})
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const handleUpload = async () => {
    if (!file || !serverConfig) return

    const maxBytes = serverConfig.max_upload_mb * 1024 * 1024
    if (file.size > maxBytes) {
      setError(
        `This file is ${(file.size / (1024 * 1024)).toFixed(0)} MB, but the limit is ` +
          `${serverConfig.max_upload_mb} MB. Try a shorter recording.`
      )
      return
    }

    const runId = ++runIdRef.current
    const stillCurrent = () => runIdRef.current === runId

    setViewedName(null)
    setLoading(true)
    setError(null)
    setPhase('uploading')
    setUploadPercent(0)
    setQueuePosition(0)
    setJobIntegrations(false)

    try {
      saveParticipants(participants)
      const { jobId } = await uploadRecording(file, participants.trim(), (percent) => {
        if (stillCurrent()) setUploadPercent(percent)
      })
      if (!stillCurrent()) return
      // The "sending results" step only shows if the user has connected at least one app.
      apiRequest('/connections')
        .then((connected) => {
          if (stillCurrent()) setJobIntegrations(Object.values(connected).some(Boolean))
        })
        .catch(() => {})
      setPhase('queued')

      await followJob(jobId, runId)
    } catch (err) {
      if (stillCurrent()) {
        setError(err.message)
        setLoading(false)
      }
    }
  }

  const handleNewMeeting = () => {
    runIdRef.current += 1 // stops any upload/polling still in progress
    setLoading(false)
    setFile(null)
    setViewedName(null)
    setResult(null)
    setError(null)
    if (fileInputRef.current) fileInputRef.current.value = ''
  }

  const handleDownloadReport = () => {
    if (!result) return

    const generatedAt = new Date().toLocaleString()
    const recordingName = file?.name || viewedName || 'Meeting'
    const reportTitle = `MeetMate Report - ${recordingName}`

    const renderList = (items, tag = 'ul') =>
      items?.length
        ? `<${tag}>${items.map((i) => `<li>${escapeHtml(itemToText(i))}</li>`).join('')}</${tag}>`
        : '<p class="empty">None detected.</p>'

    const renderTasks = (tasks) =>
      tasks?.length
        ? `<table>
            <thead><tr><th>Task</th><th>Owner</th><th>Due</th></tr></thead>
            <tbody>
              ${tasks
                .map(
                  (t) => `<tr>
                    <td>${escapeHtml(t.task || 'Untitled task')}</td>
                    <td>${escapeHtml(t.owner || 'unassigned')}</td>
                    <td>${escapeHtml(t.due_date || t.due_date_raw || 'unspecified')}</td>
                  </tr>`
                )
                .join('')}
            </tbody>
          </table>`
        : '<p class="empty">No action items detected.</p>'

    const html = `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>${escapeHtml(reportTitle)}</title>
  <style>
    body { font-family: 'Segoe UI', Roboto, Arial, sans-serif; color: #14202e; margin: 40px; line-height: 1.6; }
    h1 { color: #10263b; margin: 0 0 4px; font-size: 26px; }
    .meta { color: #667589; font-size: 13px; margin-bottom: 28px; }
    h2 { font-size: 15px; color: #0e7c7b; border-bottom: 1px solid #dde3ea; padding-bottom: 4px; margin: 26px 0 10px; }
    ul, ol { margin: 0; padding-left: 22px; }
    li { margin-bottom: 6px; }
    table { width: 100%; border-collapse: collapse; font-size: 14px; }
    th, td { border: 1px solid #dde3ea; padding: 8px 10px; text-align: left; vertical-align: top; }
    th { background: #f2f5f8; }
    tr { page-break-inside: avoid; }
    .empty { color: #667589; font-style: italic; margin: 0; }
    .transcript { white-space: pre-wrap; font-size: 12px; background: #f6f8fa; padding: 12px; border-radius: 6px; }
  </style>
</head>
<body>
  <h1>MeetMate AI - Meeting Report</h1>
  <div class="meta">Recording: ${escapeHtml(recordingName)} &middot; Generated: ${escapeHtml(generatedAt)}</div>

  <h2>Summary</h2>
  <p>${escapeHtml(result.summary || 'No summary available.')}</p>

  <h2>Agenda</h2>
  ${renderList(result.agenda, 'ol')}

  <h2>Decisions</h2>
  ${renderList(result.decisions)}

  <h2>Tasks</h2>
  ${renderTasks(result.tasks)}

  <h2>Full Transcript</h2>
  <div class="transcript">${escapeHtml(result.transcript || '')}</div>
</body>
</html>`

    const reportWindow = window.open('', '_blank')
    if (!reportWindow) {
      setError('Your browser blocked the report window. Allow pop-ups for this site and try again.')
      return
    }
    reportWindow.document.open()
    reportWindow.document.write(html)
    reportWindow.document.close()
    reportWindow.focus()
    // Short delay so the page finishes rendering before the print dialog opens.
    setTimeout(() => reportWindow.print(), 400)
  }

  const formatTime = (secs) => {
    const m = Math.floor(secs / 60)
    const s = secs % 60
    return `${m}:${s.toString().padStart(2, '0')}`
  }

  const formatFileSize = (bytes) => {
    if (!bytes) return ''
    const mb = bytes / (1024 * 1024)
    return mb >= 1 ? `${mb.toFixed(1)} MB` : `${(bytes / 1024).toFixed(0)} KB`
  }

  const transcriptLines = result ? parseTranscript(result.transcript) : []

  // The sync step only appears for jobs that are really sent on to Notion, Slack,
  // Calendar and Gmail (the owner's jobs). Guests never see it.
  const visibleSteps = PIPELINE_STEPS.filter((step) => step.key !== 'syncing' || jobIntegrations)
  const activeKey = phase === 'queued' ? 'transcribing' : phase
  const activeIndex = Math.max(
    0,
    visibleSteps.findIndex((step) => step.key === activeKey)
  )

  return (
    <>
      <header className="topbar">
        <div className="topbar-inner">
          <div className="brand">
            <span className="brand-mark">
              <Icon name="wave" size={20} />
            </span>
            MeetMate AI
          </div>
          <div className="account">
            <span className="account-email">{session.user?.email}</span>
            <button className="btn btn-small" onClick={() => setSettingsOpen(true)}>
              Connections
            </button>
            <button className="btn btn-small" onClick={() => signOut()}>
              Sign out
            </button>
          </div>
        </div>
      </header>

      {settingsOpen && <Settings onClose={() => setSettingsOpen(false)} />}

      <main className="page">
        {!result && !loading && (
          <>
            <div className="hero">
              <h1>Turn meeting recordings into minutes and tasks</h1>
              <p>
                Upload an audio or video file. MeetMate transcribes it, identifies who said what,
                and writes the summary, decisions and action items.
              </p>
            </div>

            <div
              className={`dropzone ${isDragging ? 'dragging' : ''}`}
              onDragOver={(e) => {
                e.preventDefault()
                setIsDragging(true)
              }}
              onDragLeave={() => setIsDragging(false)}
              onDrop={handleDrop}
            >
              <div className="dropzone-icon">
                <Icon name="upload" size={26} />
              </div>
              <p className="dropzone-title">Drop your recording here</p>
              <p className="dropzone-hint">or choose a file from your device</p>

              {file && (
                <div className="file-chip">
                  <span className="file-name">{file.name}</span>
                  <span className="file-size">{formatFileSize(file.size)}</span>
                </div>
              )}

              <div className="participants-field">
                <label htmlFor="participants">Who is in this meeting? (optional)</label>
                <input
                  id="participants"
                  className="field"
                  type="text"
                  autoComplete="off"
                  maxLength={300}
                  placeholder="e.g. Jarvis, Friday, Edith"
                  value={participants}
                  onChange={(e) => setParticipants(e.target.value)}
                />
                <p className="field-hint">
                  Separate names with commas, spelled as they appear on the video. This helps spell
                  names correctly and tells MeetMate how many speakers to look for.
                </p>
              </div>

              <div className="dropzone-actions">
                <label className="btn">
                  {file ? 'Choose a different file' : 'Choose file'}
                  <input
                    ref={fileInputRef}
                    className="visually-hidden"
                    type="file"
                    accept="audio/*,video/*"
                    onChange={handleFileChange}
                  />
                </label>
                <button
                  className="btn btn-primary"
                  onClick={handleUpload}
                  disabled={!file || !serverConfig}
                >
                  Process meeting
                </button>
              </div>

              <p className="formats">
                Audio or video files
                {serverConfig ? `, up to ${serverConfig.max_upload_mb} MB` : ''}, such as MP4, MP3,
                WAV or M4A.
              </p>
            </div>

            <History
              onOpen={(meeting) => {
                setFile(null)
                setViewedName(meeting.filename)
                setResult(meeting.result)
                setError(null)
              }}
            />
          </>
        )}

        {loading && (
          <div className="processing" aria-live="polite">
            <div className="processing-head">
              <div className="spinner"></div>
              <div>
                <p className="processing-title">Processing your meeting</p>
                <p className="processing-sub">Elapsed {formatTime(elapsedSeconds)}</p>
              </div>
            </div>

            {phase === 'uploading' && (
              <div
                style={{
                  height: 6,
                  borderRadius: 999,
                  background: '#e4e9ef',
                  overflow: 'hidden',
                  marginBottom: 20,
                }}
              >
                <div
                  style={{
                    height: '100%',
                    width: `${uploadPercent}%`,
                    background: 'var(--teal)',
                    transition: 'width 0.2s',
                  }}
                />
              </div>
            )}

            <ol className="steps">
              {visibleSteps.map((step, i) => (
                <li
                  key={step.key}
                  className={`step ${i < activeIndex ? 'done' : i === activeIndex ? 'active' : ''}`}
                >
                  <span className="step-dot">
                    {i < activeIndex && <Icon name="check" size={12} />}
                  </span>
                  {step.label}
                  {step.key === 'uploading' && phase === 'uploading' ? ` (${uploadPercent}%)` : ''}
                </li>
              ))}
            </ol>

            {phase === 'queued' && (
              <p className="processing-note">
                {queuePosition > 0
                  ? `Another recording is being processed. You are number ${queuePosition} in line.`
                  : 'Your recording is about to start.'}
              </p>
            )}
            {phase !== 'queued' && (
              <p className="processing-note">
                Long meetings can take several minutes. You can close this page: the meeting keeps processing and appears under Your past meetings.
              </p>
            )}
          </div>
        )}

        {error && (
          <div className="error-box" role="alert">
            <strong>Something went wrong</strong>
            {error}
          </div>
        )}

        {result && (
          <div className="results">
            <section className="minutes-head">
              <div className="head-top">
                <div className="head-info">
                  <h2 className="minutes-title">Meeting minutes</h2>
                  <div className="minutes-meta">
                    <span className="status-badge">
                      <Icon name="check" size={14} />
                      Processed
                    </span>
                    {(file || viewedName) && <span className="meta-file">{file?.name || viewedName}</span>}
                    {file && <span>{formatFileSize(file.size)}</span>}
                  </div>
                </div>
                <div className="head-actions">
                  <button className="btn btn-light" onClick={handleDownloadReport}>
                    <Icon name="download" size={18} />
                    Download report
                  </button>
                  <button className="btn btn-ghost" onClick={handleNewMeeting}>
                    <Icon name="plus" size={18} />
                    New meeting
                  </button>
                </div>
              </div>

              {result.automation_status?.length > 0 && (
                <div className="sync-row">
                  {result.automation_status.map((item) => (
                    <div
                      key={item.step}
                      className={`sync-pill sync-${item.status}`}
                      title={item.error || ''}
                    >
                      <span className="sync-icon">{STATUS_ICONS[item.status] || '?'}</span>
                      <span>{STATUS_LABELS[item.step] || item.step}</span>
                      <span className="sync-state">
                        {SYNC_STATE_LABELS[item.status] || item.status}
                      </span>
                    </div>
                  ))}
                </div>
              )}

              {result.automation_status?.some((item) => item.status !== 'ok') && (
                <div className="sync-problems">
                  {result.automation_status
                    .filter((item) => item.status !== 'ok')
                    .map((item) => (
                      <p key={item.step}>
                        <strong>{STATUS_LABELS[item.step] || item.step}:</strong>{' '}
                        {item.status === 'timed_out'
                          ? 'took too long and was skipped.'
                          : item.error || 'something went wrong.'}
                      </p>
                    ))}
                  <p>Check the connection under Connections, then process the meeting again.</p>
                </div>
              )}
            </section>

            <section className="panel panel-summary">
              <div className="panel-head">
                <h3 className="panel-title">Summary</h3>
              </div>
              <p className="summary-text">{result.summary || 'No summary was generated.'}</p>
            </section>

            <div className="two-col">
              <section className="panel">
                <div className="panel-head">
                  <h3 className="panel-title">Agenda</h3>
                  {result.agenda?.length > 0 && (
                    <span className="count-badge">{result.agenda.length}</span>
                  )}
                </div>
                {result.agenda?.length ? (
                  <ol className="agenda-list">
                    {result.agenda.map((item, i) => {
                      const { topic, note } = splitTopic(itemToText(item))
                      return (
                        <li key={i}>
                          <div>
                            {topic && <strong className="topic">{topic}</strong>}
                            <span className="note">{note}</span>
                          </div>
                        </li>
                      )
                    })}
                  </ol>
                ) : (
                  <p className="empty-note">No agenda points detected.</p>
                )}
              </section>

              <section className="panel">
                <div className="panel-head">
                  <h3 className="panel-title">Decisions</h3>
                  {result.decisions?.length > 0 && (
                    <span className="count-badge">{result.decisions.length}</span>
                  )}
                </div>
                {result.decisions?.length ? (
                  <ul className="check-list">
                    {result.decisions.map((d, i) => (
                      <li key={i}>
                        <span>{itemToText(d)}</span>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="empty-note">No decisions detected.</p>
                )}
              </section>
            </div>

            <section className="panel">
              <div className="panel-head">
                <h3 className="panel-title">Tasks</h3>
                {result.tasks?.length > 0 && (
                  <span className="count-badge">{result.tasks.length}</span>
                )}
              </div>
              {result.tasks?.length ? (
                <ul className="task-list">
                  {result.tasks.map((t, i) => {
                    const due = formatDue(t)
                    const unassigned = !t.owner || String(t.owner).toLowerCase() === 'unassigned'
                    return (
                      <li key={i} className="task-row">
                        <div>
                          <div className="task-title">{t.task || 'Untitled task'}</div>
                          <div className="task-owner-line">
                            <span className={`avatar ${unassigned ? 'avatar-unassigned' : ''}`}>
                              {getInitials(t.owner)}
                            </span>
                            {unassigned ? 'Unassigned' : t.owner}
                          </div>
                        </div>
                        <span className={`due-pill ${due ? '' : 'none'}`}>
                          {due ? `Due ${due}` : 'No due date'}
                        </span>
                      </li>
                    )
                  })}
                </ul>
              ) : (
                <p className="empty-note">No action items detected.</p>
              )}
            </section>

            <section className="panel">
              <div className="panel-head">
                <h3 className="panel-title">Full transcript</h3>
              </div>
              {transcriptLines.length ? (
                <div className="transcript" tabIndex={0} role="region" aria-label="Full transcript">
                  {transcriptLines.map((line, i) => (
                    <div key={i} className="t-line">
                      {line.speaker && <span className="t-speaker">{line.speaker}</span>}
                      {line.text}
                    </div>
                  ))}
                </div>
              ) : (
                <p className="empty-note">No transcript available.</p>
              )}
            </section>

            {mediaUrl && (
              <section className="panel panel-media">
                <div className="panel-head">
                  <h3 className="panel-title">Meeting recording</h3>
                </div>
                {file?.type?.startsWith('audio/') ? (
                  <audio className="media-player" src={mediaUrl} controls />
                ) : (
                  <video className="media-player" src={mediaUrl} controls />
                )}
              </section>
            )}
          </div>
        )}
      </main>
    </>
  )
}

export default function App() {
  const session = useSession()
  if (session === undefined) return null // still checking whether the user is signed in
  if (!session) {
    return (
      <>
        <header className="topbar">
          <div className="topbar-inner">
            <div className="brand">
              <span className="brand-mark">
                <Icon name="wave" size={20} />
              </span>
              MeetMate AI
            </div>
          </div>
        </header>
        <Login />
      </>
    )
  }
  return <Workspace session={session} key={session.user.id} />
}