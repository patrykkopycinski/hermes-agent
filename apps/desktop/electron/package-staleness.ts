/**
 * Detects when a packaged Hermes.app was built from an older commit than the
 * git checkout it was packaged from currently sits at.
 *
 * `hermes desktop` gates a rebuild on a content-hash stamp
 * (desktop-build-stamp.json) that only tracks the JS/TS source under
 * apps/desktop/. It does NOT track whether the OS-packaged .app bundle
 * (apps/desktop/release/**) was actually repackaged after that dist/ build --
 * a `--skip-build` launch, or launching the .app bundle directly
 * (Dock/Finder/relaunch) instead of via `hermes desktop`, silently runs
 * whatever was packaged last, with zero UI signal that a newer commit (and
 * its fixes) never made it into the running binary. Root-caused against
 * #53728: the clarify-prompt fix landed in dist/ (source-mode build) but the
 * packaged app.asar was one build behind and kept the pre-fix renderer code.
 *
 * install-stamp.json (electron-builder extraResources, written by
 * scripts/write-build-stamp.mjs) already records the commit the packaged
 * build was pinned to at package time. Comparing that against the checkout's
 * live HEAD is a cheap, single-owner staleness signal: if they differ, the
 * running .app predates commits the checkout already has.
 */

export interface PackageStalenessInput {
  /** commit recorded in install-stamp.json at package time, or null if unavailable */
  packagedCommit: string | null | undefined
  /** live `git rev-parse HEAD` of the checkout the .app was packaged from */
  currentCommit: string | null | undefined
}

export interface PackageStalenessResult {
  stale: boolean
  packagedCommit: string | null
  currentCommit: string | null
}

// Below this length a value can't be a real (possibly abbreviated) git SHA --
// treat it the same as missing rather than risk a false positive off a
// placeholder/fallback stamp value.
const MIN_SHA_LENGTH = 7

function isRealSha(value: string | null | undefined): value is string {
  return typeof value === 'string' && value.length >= MIN_SHA_LENGTH && /^[0-9a-f]+$/i.test(value)
}

export function computePackageStaleness({ packagedCommit, currentCommit }: PackageStalenessInput): PackageStalenessResult {
  const packaged = isRealSha(packagedCommit) ? packagedCommit : null
  const current = isRealSha(currentCommit) ? currentCommit : null

  // Compare on the shorter of the two lengths so an abbreviated SHA from one
  // source still matches its full-length counterpart from the other.
  let stale = false

  if (packaged && current) {
    const len = Math.min(packaged.length, current.length)
    stale = packaged.slice(0, len).toLowerCase() !== current.slice(0, len).toLowerCase()
  }

  return { stale, packagedCommit: packaged, currentCommit: current }
}
