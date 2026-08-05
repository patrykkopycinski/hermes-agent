import assert from 'node:assert/strict'

import { test } from 'vitest'

import { computePackageStaleness } from './package-staleness'

test('flags stale when packaged commit predates the checkout HEAD', () => {
  const result = computePackageStaleness({
    packagedCommit: '9ecacd6bf414d272b40ac5756650fc1014143e8f',
    currentCommit: '9e28824606a5b636bf06d7aa29e1690b074f72f8'
  })

  assert.equal(result.stale, true)
  assert.equal(result.packagedCommit, '9ecacd6bf414d272b40ac5756650fc1014143e8f')
  assert.equal(result.currentCommit, '9e28824606a5b636bf06d7aa29e1690b074f72f8')
})

test('not stale when packaged commit matches the checkout HEAD', () => {
  const sha = '9e28824606a5b636bf06d7aa29e1690b074f72f8'

  assert.equal(computePackageStaleness({ packagedCommit: sha, currentCommit: sha }).stale, false)
})

test('not stale when packaged commit case differs but matches otherwise', () => {
  assert.equal(
    computePackageStaleness({
      packagedCommit: '9E28824606A5B636BF06D7AA29E1690B074F72F8',
      currentCommit: '9e28824606a5b636bf06d7aa29e1690b074f72f8'
    }).stale,
    false
  )
})

test('matches on shared prefix when one commit is abbreviated', () => {
  assert.equal(
    computePackageStaleness({
      packagedCommit: '9e28824',
      currentCommit: '9e28824606a5b636bf06d7aa29e1690b074f72f8'
    }).stale,
    false
  )
})

test('not stale when either commit is unavailable', () => {
  assert.equal(computePackageStaleness({ packagedCommit: null, currentCommit: 'abc1234' }).stale, false)
  assert.equal(computePackageStaleness({ packagedCommit: 'abc1234', currentCommit: undefined }).stale, false)
  assert.equal(computePackageStaleness({ packagedCommit: null, currentCommit: null }).stale, false)
})

test('treats non-sha-shaped values (placeholders, empty, too short) as unavailable', () => {
  assert.equal(
    computePackageStaleness({
      packagedCommit: '0000000000000000000000000000000000000000',
      currentCommit: '0000000000000000000000000000000000000000'
    }).packagedCommit,
    '0000000000000000000000000000000000000000'
  )
  assert.equal(computePackageStaleness({ packagedCommit: 'abc', currentCommit: '9e28824606a5b636' }).packagedCommit, null)
  assert.equal(computePackageStaleness({ packagedCommit: '', currentCommit: '9e28824606a5b636' }).stale, false)
  assert.equal(computePackageStaleness({ packagedCommit: 'not-a-sha!', currentCommit: '9e28824606a5b636' }).packagedCommit, null)
})
