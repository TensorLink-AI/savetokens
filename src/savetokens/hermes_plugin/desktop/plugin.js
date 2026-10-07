// savetokens status-bar item for Hermes Desktop: actual -> expected for your limits and spend.
// Data comes from this package's backend route (dashboard/plugin_api.py), polled every 2 minutes.
import { host, STATUSBAR_AREAS } from '@hermes/plugin-sdk'
import { useEffect, useState } from 'react'
import { jsx } from 'react/jsx-runtime'

const POLL_MS = 120000

function SaveTokensStatus({ ctx }) {
  const [data, setData] = useState({ text: 'savetokens', detail: '' })

  useEffect(() => {
    let live = true
    const load = () =>
      ctx
        .rest('/status')
        .then(d => { if (live && d && d.text) setData(d) })
        .catch(() => {})   // backend half disabled or gateway down: keep the last value
    load()
    const id = setInterval(load, POLL_MS)
    return () => { live = false; clearInterval(id) }
  }, [])

  return jsx('button', {
    type: 'button',
    title: data.detail,
    className: 'px-1.5 text-[0.6875rem] text-(--ui-text-tertiary)',
    onClick: () => host.notify({ kind: 'info', message: data.detail || data.text }),
    children: data.text
  })
}

export default {
  id: 'savetokens',
  name: 'savetokens',
  register(ctx) {
    ctx.register({
      id: 'status',
      area: STATUSBAR_AREAS.right,
      order: 140,
      render: () => jsx(SaveTokensStatus, { ctx })
    })
  }
}
