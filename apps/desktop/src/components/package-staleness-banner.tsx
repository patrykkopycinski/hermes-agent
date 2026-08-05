import { useEffect } from 'react'

import { translateNow } from '@/i18n'
import { notify } from '@/store/notifications'

// Boot-time signal for a packaged .app whose bundled code predates the
// checkout it was packaged from (see electron/package-staleness.ts for the
// full root-cause writeup — #53728's clarify-prompt fix shipped to dist/ one
// build ahead of the packaged app.asar because the app was launched directly
// instead of via `hermes desktop`, the only path that re-checks the build
// stamp). Runs once per launch, mirrors RemoteDisplayBanner's shape: a single
// IPC call on mount, a persistent toast through the shared notification stack
// when the check comes back positive, silent otherwise.
export function PackageStalenessBanner() {
  useEffect(() => {
    void window.hermesDesktop?.getPackageStaleness?.().then(result => {
      if (!result?.stale) {
        return
      }

      notify({
        durationMs: 0,
        kind: 'warning',
        message: translateNow(
          'packageStalenessBanner.message',
          result.packagedCommit?.slice(0, 12) ?? '',
          result.currentCommit?.slice(0, 12) ?? ''
        ),
        placement: 'default'
      })
    })
  }, [])

  return null
}
