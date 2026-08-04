import type { ThreadMessage, ToolCallMessagePartProps } from '@assistant-ui/react'
import { AssistantRuntimeProvider, useExternalStoreRuntime } from '@assistant-ui/react'
import { cleanup, render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { I18nProvider } from '@/i18n'

import { ClarifyTool, readClarifyResult } from './clarify-tool'
import { Thread } from './thread'

// DOM APIs assistant-ui's Thread/viewport (use-stick-to-bottom) relies on but
// jsdom doesn't implement — see approval-group.test.tsx for the same stubs.
class TestResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

vi.stubGlobal('ResizeObserver', TestResizeObserver)
vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) =>
  window.setTimeout(() => callback(performance.now()), 0)
)
vi.stubGlobal('cancelAnimationFrame', (id: number) => window.clearTimeout(id))

Element.prototype.scrollTo = function scrollTo() {}

Element.prototype.animate = function animate() {
  return {
    cancel: () => {},
    finished: Promise.resolve()
  } as unknown as Animation
}

afterEach(() => {
  cleanup()
})

function renderClarify(ui: ReactNode) {
  return render(
    <I18nProvider configClient={null} initialLocale="en">
      {ui}
    </I18nProvider>
  )
}

function settledClarifyProps(
  args: ToolCallMessagePartProps['args'],
  result: ToolCallMessagePartProps['result'],
  toolCallId: string
): ToolCallMessagePartProps {
  return {
    addResult: vi.fn(),
    args,
    argsText: JSON.stringify(args),
    isError: false,
    respondToApproval: vi.fn(),
    result,
    resume: vi.fn(),
    status: { type: 'complete' },
    toolCallId,
    toolName: 'clarify',
    type: 'tool-call'
  }
}

describe('readClarifyResult', () => {
  it('reads question + user_response from the tool JSON payload', () => {
    expect(
      readClarifyResult({
        question: 'Which target?',
        choices_offered: ['staging', 'prod'],
        user_response: 'staging'
      })
    ).toEqual({
      question: 'Which target?',
      answer: 'staging',
      error: undefined
    })
  })

  it('parses a JSON string result the same way as an object', () => {
    expect(
      readClarifyResult(
        JSON.stringify({
          question: 'Ship it?',
          user_response: 'yes'
        })
      )
    ).toEqual({
      question: 'Ship it?',
      answer: 'yes',
      error: undefined
    })
  })

  it('keeps an empty user_response so Skip can render as skipped', () => {
    expect(readClarifyResult({ question: 'Ok?', user_response: '' })).toEqual({
      question: 'Ok?',
      answer: '',
      error: undefined
    })
  })
})

describe('ClarifyTool settled view', () => {
  it('keeps the question and answer visible after the tool completes', () => {
    renderClarify(
      <ClarifyTool
        {...settledClarifyProps(
          { question: 'Which deployment target?', choices: ['staging', 'prod'] },
          {
            question: 'Which deployment target?',
            choices_offered: ['staging', 'prod'],
            user_response: 'staging'
          },
          'clarify-1'
        )}
      />
    )

    expect(screen.getByText('Which deployment target?')).toBeTruthy()
    expect(screen.getByText('staging')).toBeTruthy()
    expect(document.querySelector('[data-clarify-settled]')).toBeTruthy()
  })

  it('renders every offered choice, marking the one the user picked', () => {
    renderClarify(
      <ClarifyTool
        {...settledClarifyProps(
          { question: 'Which deployment target?', choices: ['staging', 'prod'] },
          {
            question: 'Which deployment target?',
            choices_offered: ['staging', 'prod'],
            user_response: 'staging'
          },
          'clarify-3'
        )}
      />
    )

    const choiceRows = document.querySelectorAll('[data-choice]')
    expect(choiceRows).toHaveLength(2)
    expect(screen.getByText('staging')).toBeTruthy()
    expect(screen.getByText('prod')).toBeTruthy()
    expect(document.querySelector('[data-choice][data-picked]')?.textContent).toContain('staging')
  })

  it('shows the freeform answer separately when it does not match any offered choice', () => {
    renderClarify(
      <ClarifyTool
        {...settledClarifyProps(
          { question: 'Which deployment target?', choices: ['staging', 'prod'] },
          {
            question: 'Which deployment target?',
            choices_offered: ['staging', 'prod'],
            user_response: 'canary'
          },
          'clarify-4'
        )}
      />
    )

    expect(document.querySelectorAll('[data-choice]')).toHaveLength(2)
    expect(document.querySelector('[data-choice][data-picked]')).toBeNull()
    expect(document.querySelector('[data-clarify-answer]')?.textContent).toBe('canary')
  })

  it('labels an empty response as Skipped', () => {
    renderClarify(
      <ClarifyTool
        {...settledClarifyProps(
          { question: 'Anything else?' },
          { question: 'Anything else?', user_response: '' },
          'clarify-2'
        )}
      />
    )

    expect(screen.getByText('Anything else?')).toBeTruthy()
    expect(screen.getByText('Skipped')).toBeTruthy()
  })
})

// Regression coverage for the "reopened session shows only 'Asked a question'
// with no detail" bug. A clarify tool-call with no result, on a message whose
// *thread* is no longer running (app/gateway restarted, session reopened from
// history, or the turn was otherwise interrupted before an answer arrived),
// used to fall through to the generic ToolFallback — which has no
// clarify-specific view and collapsed straight to the bare "Asked a question"
// label, dropping the question text and every offered choice. It must now
// render the question + choices read-only instead.
describe('ClarifyTool expired view (message stopped with no result)', () => {
  const createdAt = new Date('2026-07-24T23:44:45.000Z')

  function expiredClarifyMessage(): ThreadMessage {
    return {
      id: 'assistant-clarify-expired',
      role: 'assistant',
      content: [
        {
          type: 'tool-call',
          toolCallId: 'clarify-expired-1',
          toolName: 'clarify',
          args: { question: 'Which target?', choices: ['staging', 'prod'] },
          argsText: JSON.stringify({ question: 'Which target?', choices: ['staging', 'prod'] })
        }
      ],
      status: { type: 'complete', reason: 'stop' },
      createdAt,
      metadata: {
        unstable_state: null,
        unstable_annotations: [],
        unstable_data: [],
        steps: [],
        custom: {}
      }
    } as ThreadMessage
  }

  function ExpiredClarifyHarness() {
    const runtime = useExternalStoreRuntime<ThreadMessage>({
      messages: [expiredClarifyMessage()],
      isRunning: false,
      onNew: async () => {}
    })

    return (
      <AssistantRuntimeProvider runtime={runtime}>
        <Thread />
      </AssistantRuntimeProvider>
    )
  }

  it('renders the question and offered choices instead of collapsing to a bare label', () => {
    renderClarify(<ExpiredClarifyHarness />)

    expect(screen.getByText('Which target?')).toBeTruthy()
    expect(screen.getByText('staging')).toBeTruthy()
    expect(screen.getByText('prod')).toBeTruthy()
    expect(document.querySelector('[data-clarify-expired]')).toBeTruthy()
    expect(document.querySelectorAll('[data-clarify-expired] [data-choice]')).toHaveLength(2)
  })
})
