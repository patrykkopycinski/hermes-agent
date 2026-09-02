/**
 * wfgraph run viewer — a read-only pane over the workflow engine's runs.
 *
 * Plain ESM, loaded uncompiled: UI is jsx() calls, no JSX syntax. Data comes
 * from this plugin's own Python backend (~/.hermes/plugins/wfgraph/dashboard/
 * plugin_api.py), which reads through the engine's own store so the pane sees
 * exactly what the runner sees -- including a run whose process died being
 * reported as failed rather than a phantom "running".
 *
 * Polling, not sockets: ctx.socket is a no-op on OAuth remotes, so useQuery
 * with refetchInterval is the portable choice.
 */

import {
  Badge,
  Codicon,
  EmptyState,
  ErrorState,
  ScrollArea,
  Separator,
  Skeleton,
  StatusDot,
  cn,
  useQuery,
  useValue,
  atom
} from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'
import { Fragment, useEffect, useState } from 'react'

const ID = 'wfgraph-viewer'

// Which run the detail half is showing. Imperative reads in handlers.
const $selected = atom(null)

const TERMINAL = new Set(['succeeded', 'failed', 'cancelled'])

// StatusDot tones are good | muted | warn | bad (see components/status-dot.tsx).
function statusTone(status) {
  if (status === 'succeeded') return 'good'
  if (status === 'failed') return 'bad'
  if (status === 'cancelled') return 'warn'
  if (status === 'waiting_world' || status === 'paused') return 'warn'
  return 'muted'
}

// Badge takes `variant`: default | muted | success | warn | destructive | outline | solid.
function statusBadge(status) {
  if (status === 'succeeded') return 'success'
  if (status === 'failed') return 'destructive'
  if (status === 'cancelled' || status === 'waiting_world' || status === 'paused') return 'warn'
  return 'muted'
}

function stepGlyph(state) {
  if (state === 'ran') return 'pass-filled'
  if (state === 'running') return 'sync'
  if (state === 'stopped') return 'warning'
  if (state === 'skipped') return 'circle-slash'
  return 'circle-outline'
}

function stepTone(state, verdict) {
  if (state === 'running') return 'text-(--ui-accent)'
  if (state === 'stopped') return 'text-(--ui-danger)'
  if (state === 'skipped') return 'text-(--ui-text-quaternary)'
  if (state === 'ran' && verdict === 'FAIL') return 'text-(--ui-danger)'
  if (state === 'ran') return 'text-(--ui-text-secondary)'
  return 'text-(--ui-text-tertiary)'
}

function relTime(ms) {
  if (!ms) return ''
  const secs = Math.max(0, Math.round((Date.now() - ms) / 1000))
  if (secs < 60) return `${secs}s ago`
  if (secs < 3600) return `${Math.round(secs / 60)}m ago`
  if (secs < 86400) return `${Math.round(secs / 3600)}h ago`
  return `${Math.round(secs / 86400)}d ago`
}

function RunRow({ run, selected, onPick }) {
  return jsxs('button', {
    type: 'button',
    onClick: () => onPick(run.runId),
    className: cn(
      'flex w-full flex-col gap-0.5 px-2 py-1.5 text-left transition-colors',
      'hover:bg-(--chrome-action-hover)',
      selected && 'bg-(--chrome-action-hover)'
    ),
    children: [
      jsxs('div', {
        className: 'flex items-center gap-1.5',
        children: [
          jsx(StatusDot, { tone: statusTone(run.status) }),
          jsx('span', {
            className: 'truncate text-xs font-medium',
            children: run.workflow || run.runId
          }),
          jsx('span', {
            className: 'ml-auto shrink-0 text-[0.6875rem] text-(--ui-text-quaternary)',
            children: relTime(run.startedAt)
          })
        ]
      }),
      jsxs('div', {
        className: 'flex items-center gap-1.5 text-[0.6875rem] text-(--ui-text-tertiary)',
        children: [
          jsx('span', { children: run.status }),
          run.loops
            ? jsx('span', { children: `· ${run.loops} loop${run.loops === 1 ? '' : 's'}` })
            : null,
          jsx('span', { children: `· ${run.ran.length} ran` })
        ]
      })
    ]
  })
}

function StepRow({ step }) {
  return jsxs('div', {
    className: 'flex items-start gap-1.5 py-1',
    children: [
      jsx(Codicon, {
        name: stepGlyph(step.state),
        className: cn('mt-0.5 shrink-0 text-[0.75rem]', stepTone(step.state, step.verdict))
      }),
      jsxs('div', {
        className: 'flex min-w-0 flex-col',
        children: [
          jsxs('div', {
            className: 'flex items-center gap-1.5',
            children: [
              jsx('span', {
                className: cn('truncate text-xs', stepTone(step.state, step.verdict)),
                children: step.title
              }),
              jsx('span', {
                className: 'shrink-0 text-[0.625rem] text-(--ui-text-quaternary)',
                children: step.kind
              }),
              step.verdict
                ? jsx(Badge, {
                    variant: step.verdict === 'PASS' ? 'success' : 'destructive',
                    size: 'xs',
                    children: step.verdict
                  })
                : null
            ]
          }),
          step.summary
            ? jsx('span', {
                className: 'line-clamp-2 text-[0.6875rem] text-(--ui-text-tertiary)',
                children: step.summary
              })
            : null
        ]
      })
    ]
  })
}

// Event types the engine actually emits, verified against run .jsonl files:
// RunStarted, NodePending, NodeStarted, NodeFinished, AgentTraceSummary,
// TaskOutput, GateEvaluated, RunFinished.
const EVENT_TONE = {
  RunFinished: 'text-(--ui-text-secondary)',
  GateEvaluated: 'text-(--ui-accent)',
  AgentTraceSummary: 'text-(--ui-text-secondary)'
}

// One line per event. The payload shape differs per type, so each is read
// explicitly rather than stringified -- a raw JSON dump is noise, not a feed.
function eventLine(ev) {
  const p = ev.payload || {}
  switch (ev.type) {
    case 'RunStarted':
      return `started ${p.scenario || ''}`.trim()
    case 'RunFinished':
      return `finished ${p.state || ''}`.trim()
    case 'NodePending':
      return `${p.nodeId} queued`
    case 'NodeStarted':
      return `${p.nodeId} started`
    case 'NodeFinished':
      return `${p.nodeId} done`
    case 'AgentTraceSummary':
      return `${p.nodeId} ${p.verdict || ''}`.trim()
    case 'TaskOutput':
      return `${p.nodeId} output`
    case 'GateEvaluated':
      // The most useful line in the feed: why the gate routed where it did.
      return p.summary ? `${p.nodeId}: ${p.summary}` : `${p.nodeId} evaluated`
    default:
      return ev.type
  }
}

function EventFeed({ events }) {
  if (!events || !events.length) return null
  // Newest first: the interesting end of a long run is the tail.
  const rows = events.slice().reverse()
  return jsxs('div', {
    className: 'flex flex-col gap-0.5',
    children: [
      jsx('div', {
        className: 'text-[0.625rem] font-medium text-(--ui-text-quaternary)',
        children: `events (${events.length})`
      }),
      ...rows.map(ev =>
        jsxs('div', {
          className: 'flex items-baseline gap-1.5 font-mono text-[0.625rem]',
          children: [
            jsx('span', {
              className: 'shrink-0 text-(--ui-text-quaternary)',
              children: String(ev.seq).padStart(2, '0')
            }),
            jsx('span', {
              className: cn('truncate', EVENT_TONE[ev.type] || 'text-(--ui-text-tertiary)'),
              children: eventLine(ev)
            })
          ]
        }, ev.seq)
      )
    ]
  })
}

function Detail({ ctx, runId }) {
  const [live, setLive] = useState(true)

  const { data, error, isLoading } = useQuery({
    queryKey: [ID, 'run', runId],
    queryFn: () => ctx.rest(`/runs/${encodeURIComponent(runId)}`),
    // Match the app's convention: a plain number, flipped by observed state,
    // rather than the function form. Stop polling once the run is terminal.
    refetchInterval: live ? 2000 : false,
    enabled: Boolean(runId)
  })

  // A terminal run never changes again -- stop hitting the backend for it.
  useEffect(() => {
    setLive(!TERMINAL.has(data?.status))
  }, [data?.status])

  if (isLoading) return jsx(Skeleton, { className: 'h-24 w-full' })
  if (error) return jsx(ErrorState, { title: 'Could not load run', description: String(error) })
  if (!data || data.error) {
    return jsx(EmptyState, { title: data?.error || 'No run selected' })
  }

  return jsxs('div', {
    className: 'flex flex-col gap-1.5 p-2',
    children: [
      jsxs('div', {
        className: 'flex items-center gap-1.5',
        children: [
          jsx(StatusDot, { tone: statusTone(data.status) }),
          jsx('span', { className: 'text-xs font-medium', children: data.workflow }),
          jsx(Badge, { variant: statusBadge(data.status), size: 'xs', children: data.status })
        ]
      }),
      jsx('div', {
        className: 'font-mono text-[0.625rem] text-(--ui-text-quaternary)',
        children: data.runId
      }),

      // The whole reason defect #12 exists: a failed run must say why.
      data.error
        ? jsx('div', {
            className: 'rounded-sm border border-(--ui-danger)/40 px-1.5 py-1 text-[0.6875rem] text-(--ui-danger)',
            children: data.error
          })
        : null,

      data.parkedOn
        ? jsx('div', {
            className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
            children: `parked on ${data.parkedOn}${data.parkedUntil ? ` until ${data.parkedUntil}` : ''}`
          })
        : null,

      jsx(Separator, {}),
      jsx('div', {
        className: 'flex flex-col',
        children: (data.steps || []).map(s => jsx(StepRow, { step: s }, s.id))
      }),

      data.events && data.events.length
        ? jsxs(Fragment, {
            children: [jsx(Separator, {}), jsx(EventFeed, { events: data.events })]
          })
        : null
    ]
  })
}

function ViewerPane({ ctx }) {
  const selected = useValue($selected)

  const { data, error, isLoading } = useQuery({
    queryKey: [ID, 'runs'],
    queryFn: () => ctx.rest('/runs'),
    refetchInterval: 3000
  })

  if (isLoading) {
    return jsx('div', { className: 'p-2', children: jsx(Skeleton, { className: 'h-32 w-full' }) })
  }
  if (error) {
    return jsx(ErrorState, {
      title: 'wfgraph backend unreachable',
      // Two causes, in the order they actually bite. The dashboard mounts
      // plugin routes at STARTUP, so a backend installed while it was already
      // running is invisible until restart -- that is the common case, and the
      // one that cost real debugging time. config.yaml is the rarer cause.
      description:
        'Restart the dashboard (hermes dashboard) -- plugin routes mount at ' +
        'startup, so a backend added later is not served yet. If that does not ' +
        'fix it, check that wfgraph is in plugins.enabled in config.yaml.'
    })
  }

  const runs = data?.runs || []
  if (!runs.length) {
    return jsx(EmptyState, {
      title: 'No workflow runs yet',
      description: 'Start one with the wfgraph tool, or a cron/webhook trigger.'
    })
  }

  const active = selected && runs.some(r => r.runId === selected) ? selected : runs[0].runId

  return jsxs('div', {
    className: 'flex h-full flex-col',
    children: [
      jsx('div', {
        className: 'shrink-0 px-2 py-1 text-[0.625rem] font-medium tracking-wide text-(--ui-text-quaternary) uppercase',
        children: `runs · ${runs.length}`
      }),
      jsx(ScrollArea, {
        className: 'max-h-[40%] shrink-0',
        children: jsx('div', {
          className: 'flex flex-col',
          children: runs.map(r =>
            jsx(
              RunRow,
              { run: r, selected: r.runId === active, onPick: id => $selected.set(id) },
              r.runId
            )
          )
        })
      }),
      jsx(Separator, {}),
      jsx(ScrollArea, {
        className: 'min-h-0 flex-1',
        children: jsx(Detail, { ctx, runId: active })
      })
    ]
  })
}

export default {
  id: ID,
  name: 'wfgraph runs',
  register(ctx) {
    ctx.i18n.register({
      en: {
        paneTitle: 'wfgraph runs'
      }
    })

    ctx.register({
      id: 'pane',
      area: 'panes',
      title: 'wfgraph runs',
      data: { placement: 'right', width: '300px' },
      render: () => jsx(ViewerPane, { ctx })
    })
  }
}
