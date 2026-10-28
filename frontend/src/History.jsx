import { useEffect, useState } from 'react'
import { apiRequest } from './api'

export default function History({ onOpen }) {
  const [items, setItems] = useState(null)
  const [error, setError] = useState(null)
  const [openingId, setOpeningId] = useState(null)

  useEffect(() => {
    apiRequest('/meetings')
      .then(setItems)
      .catch((err) => setError(err.message))
  }, [])

  if (error || !items || items.length === 0) return null

  const open = async (item) => {
    setOpeningId(item.id)
    try {
      const meeting = await apiRequest(`/meetings/${item.id}`)
      onOpen(meeting)
    } catch (err) {
      setError(err.message)
    } finally {
      setOpeningId(null)
    }
  }

  const remove = async (item) => {
    if (!window.confirm(`Delete "${item.filename}" from your history?`)) return
    try {
      await apiRequest(`/meetings/${item.id}`, { method: 'DELETE' })
      setItems((list) => list.filter((m) => m.id !== item.id))
    } catch (err) {
      setError(err.message)
    }
  }

  return (
    <section className="panel history">
      <div className="panel-head">
        <h3 className="panel-title">Your past meetings</h3>
        <span className="count-badge">{items.length}</span>
      </div>
      <ul className="history-list">
        {items.map((item) => (
          <li key={item.id} className="history-row">
            <button className="history-open" onClick={() => open(item)} disabled={openingId === item.id}>
              <span className="history-name">{item.filename}</span>
              <span className="history-meta">
                {new Date(item.created_at).toLocaleString()} · {item.task_count} task
                {item.task_count === 1 ? '' : 's'}
              </span>
            </button>
            <button className="btn history-delete" onClick={() => remove(item)} aria-label={`Delete ${item.filename}`}>
              Delete
            </button>
          </li>
        ))}
      </ul>
    </section>
  )
}
