import { useCallback, useEffect, useRef, useState } from 'react'
import type { FormEvent, ReactNode } from 'react'
import { Activity, ArrowDownToLine, ArrowRight, Box, Check, ChevronRight, CircleHelp, CircuitBoard, Cloud, Cpu, FileUp, FlaskConical, History, LayoutDashboard, LockKeyhole, LogOut, Network, Plus, Radio, RefreshCw, Rocket, Server, ShieldCheck, X } from 'lucide-react'
import { ApiError } from './api'
import { createRuntime } from './auth'
import type { Runtime } from './auth'
import { connected, deployBlock, DEVICE, GATEWAY, phaseLabel, prepareUpload, progress, validateFile } from './domain'
import type { Deployment, Snapshot } from './domain'
import { DeploymentRequest } from './deployment'
import './App.css'
import './theme.css'

const empty: Snapshot = { devices: [], releases: [], deployments: [] }
const dateTime = (value: string | null) => value ? new Date(value).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' }) : 'Not observed'
const safeError = (error: unknown) => error instanceof ApiError ? error.message : 'The operation could not be completed. Refresh and review before continuing.'
let boot: Promise<Runtime> | undefined

export default function App() {
  const [runtime, setRuntime] = useState<Runtime | null>(null)
  const [failed, setFailed] = useState(false)
  useEffect(() => {
    let mounted = true
    boot ??= createRuntime()
    void boot.then(value => { if (mounted) setRuntime(value) }, () => { if (mounted) setFailed(true) })
    return () => { mounted = false }
  }, [])
  if (failed) return <Entry title="Console unavailable" description="Runtime configuration or sign-in could not be validated. Check the frontend configuration and registered callback, then try again."><button onClick={() => location.reload()}>Try again</button><a className="text-link" href="?demo=1">Explore the isolated demo <ArrowRight size={16} /></a></Entry>
  if (!runtime) return <Entry title="Opening your workspace" description="Checking configuration and session…"><span className="loader" /></Entry>
  if (!runtime.authenticated) return <SignIn runtime={runtime} />
  return <Console runtime={runtime} />
}
function Entry({ title, description, children }: { title: string; description: string; children: ReactNode }) {
  return <main className="entry"><div className="entry-art"><Brand /><div className="orb"><CircuitBoard size={100} strokeWidth={1} /></div><h1>Connected devices.<br />Confident releases.</h1><p>A clear view of your edge, from observation to verified application.</p><span>EDGE OPERATIONS / ADMIN CONSOLE</span></div><section className="entry-card"><ShieldCheck size={32} /><p className="eyebrow">SECURE WORKSPACE</p><h2>{title}</h2><p>{description}</p>{children}</section></main>
}
function SignIn({ runtime }: { runtime: Runtime }) {
  const [error, setError] = useState(false)
  const [busy, setBusy] = useState(false)
  return <Entry title="Welcome to cloud demo" description="Sign in with your administrator account to manage devices and firmware releases.">
    {error && <p role="alert">Sign-in could not be started. Please try again.</p>}
    <button disabled={busy} onClick={() => { setBusy(true); void runtime.login().catch(() => { setError(true); setBusy(false) }) }}><LockKeyhole size={17} />{busy ? 'Redirecting…' : 'Sign in securely'}<ArrowRight size={17} /></button>
    <small>Cognito authentication · Authorization code + PKCE</small><a className="text-link" href="?demo=1">Explore the isolated demo <ArrowRight size={16} /></a>
  </Entry>
}
function Brand() { return <div className="brand"><span className="brand-icon"><Cloud size={23} /></span><strong>cloud demo</strong></div> }
function Badge({ children, tone = '' }: { children: ReactNode; tone?: string }) { return <span className={`badge ${tone}`}><span className="status-dot" />{children}</span> }

export function Console({ runtime }: { runtime: Runtime }) {
  const [page, setPage] = useState<'Overview' | 'Devices' | 'Firmware' | 'Deployments'>('Overview')
  const [data, setData] = useState<Snapshot>(empty)
  const [loaded, setLoaded] = useState(false)
  const [now, setNow] = useState(() => Date.now())
  const [lastSync, setLastSync] = useState<string | null>(null)
  const [pollError, setPollError] = useState('')
  const [authExpired, setAuthExpired] = useState(false)
  const [notice, setNotice] = useState('')
  const [selected, setSelected] = useState<string>(DEVICE)
  const [releaseId, setReleaseId] = useState('')
  const [modal, setModal] = useState<'upload' | 'deploy' | null>(null)
  const [busy, setBusy] = useState(false)
  const [refreshCount, setRefreshCount] = useState(0)
  const [requestState] = useState(() => {
    try { return { request: new DeploymentRequest(sessionStorage, runtime.storageKey), error: false } }
    catch { return { request: null, error: true } }
  })
  const [pending, setPending] = useState(requestState.request?.current ?? null)
  const mutation = useRef<AbortController | null>(null)
  const refresh = useCallback(() => setRefreshCount(value => value + 1), [])
  useEffect(() => () => { mutation.current?.abort() }, [])
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [])
  useEffect(() => {
    if (authExpired) return
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout>
    async function poll() {
      try {
        const next = await runtime.api.snapshot(controller.signal)
        if (controller.signal.aborted) return
        if (!mutation.current) await requestState.request?.reconcileArm(runtime.api, controller.signal)
        if (controller.signal.aborted) return
        requestState.request?.reconcile(next.deployments.map(j => j.command_id))
        setPending(requestState.request?.current ?? null)
        setData(next); setNow(Date.now()); setLoaded(true); setPollError(''); setLastSync(new Date().toISOString())
      } catch (error) {
        if (controller.signal.aborted) return
        setPollError(safeError(error))
        if (error instanceof ApiError && error.unauthorized) setAuthExpired(true)
      } finally {
        if (!controller.signal.aborted) timer = setTimeout(() => void poll(), 5000)
      }
    }
    void poll()
    return () => { controller.abort(); clearTimeout(timer) }
  }, [runtime, refreshCount, requestState, authExpired])

  const device = data.devices.find(d => d.device_id === selected)
  const chosenRelease = data.releases.find(r => r.release_id === releaseId)
  const selectedRelease = pending?.release_id ?? releaseId
  const blocked = authExpired ? 'Sign in again to continue.' : requestState.error ? 'Session storage is unavailable or a saved request is invalid. Deployment is locked for safety.' : pollError ? 'Refresh successfully before deploying.' : !loaded ? 'Waiting for device observations.' : deployBlock(data, selected, selectedRelease, now)
  const canSend = requestState.request?.canSend(device?.firmware, now) ?? false
  const canAbandon = requestState.request?.canAbandon(now) ?? false
  const armMessage = pending?.stage === 'deploy_pending' ? 'Deployment response uncertain. Reconcile deployment history; do not send again or start a new request.'
    : pending?.stage === 'arm_pending' ? 'Arm response uncertain. Refresh to check this request; no deployment can be sent until the arm is verified.'
    : pending?.stage === 'armed' ? canAbandon ? 'Arm expired. No deployment was sent. Abandon this unused arm, then review and arm again.'
      : `Armed · ${Math.max(0, Math.ceil((Date.parse(pending.arm.expires_at) - now) / 1000))}s remaining. Arming does not publish or install firmware.` : ''
  const sortedJobs = [...data.deployments].sort((a, b) => b.created_at.localeCompare(a.created_at))
  const gateways = data.devices.filter(d => d.kind === 'gateway')
  const subdevices = data.devices.filter(d => d.kind === 'subdevice')
  const onlineCount = (items: typeof data.devices) => items.filter(d => connected(d, data.devices, now)).length

  async function action(operation: (signal: AbortSignal) => Promise<unknown>, message: string) {
    if (mutation.current) return
    const controller = new AbortController()
    mutation.current = controller; setBusy(true); setNotice('')
    try {
      await operation(controller.signal)
      if (!controller.signal.aborted) { setNotice(message); refresh() }
    } catch (error) {
      if (!controller.signal.aborted) {
        setNotice(safeError(error))
        if (error instanceof ApiError && error.unauthorized) setAuthExpired(true)
      }
    } finally {
      mutation.current = null
      if (!controller.signal.aborted) { setBusy(false); setPending(requestState.request?.current ?? null) }
    }
  }
  function armDeployment() {
    // eslint-disable-next-line react-hooks/purity -- click handler must recheck actual observation age, not render time.
    if (blocked || pending || !requestState.request || deployBlock(data, selected, selectedRelease, Date.now())) return
    void action(signal => requestState.request!.arm(runtime.api, selectedRelease, signal), 'Deployment armed. Review the versions, then separately confirm and send before expiry.')
  }
  function abandonArm() {
    if (busy || authExpired || !requestState.request?.canAbandon()) return
    try { requestState.request.abandon(); setPending(null); setModal('deploy'); setNotice('') }
    catch { setNotice('The saved request could not be cleared. Deployment remains locked.') }
  }
  function signOut() {
    setAuthExpired(true); setModal(null)
    void action(() => runtime.logout(), 'Signed out.')
  }
  function confirmDeploy() {
    // Recheck at confirmation; the modal never freezes observation freshness.
    // eslint-disable-next-line react-hooks/purity -- click handler must recheck actual observation age, not render time.
    if (blocked || !requestState.request?.canSend(device?.firmware) || deployBlock(data, selected, selectedRelease, Date.now())) return
    setModal(null)
    void action(async signal => {
      const job = await requestState.request!.submit(runtime.api, device?.firmware, signal)
      if (!signal.aborted) {
        setData(previous => ({ ...previous, deployments: [job, ...previous.deployments.filter(j => j.command_id !== job.command_id)] }))
        setPage('Deployments')
      }
    }, runtime.demo ? 'Simulated deployment created. No command was sent.' : 'Request recorded. Follow physical prompts in Pi journal. Dispatch alone is not verification.')
  }

  return <div className="app-shell">
    <aside className="sidebar"><Brand /><div className="workspace"><span className="workspace-avatar">L</span><div><strong>Lab workspace</strong><small>Device operations</small></div><LockKeyhole size={14} /></div><p className="nav-label">WORKSPACE</p>
      <nav aria-label="Main navigation">{([{ name: 'Overview', icon: LayoutDashboard }, { name: 'Devices', icon: Cpu }, { name: 'Firmware', icon: Box }, { name: 'Deployments', icon: History }] as const).map(({ name, icon: Icon }) => <button key={name} className={`nav-item ${page === name ? 'active' : ''}`} aria-current={page === name ? 'page' : undefined} onClick={() => setPage(name)}><Icon size={19} />{name}{name === 'Devices' && <span className="nav-count">{data.devices.length || '—'}</span>}</button>)}
        <div className="nav-divider" /><button className="nav-item" disabled><Plus size={19} />Onboarding<span className="soon">Soon</span></button><button className="nav-item" disabled><Activity size={19} />Telemetry<span className="soon">Soon</span></button>
      </nav>
      <div className="sidebar-bottom"><div className="security-note"><ShieldCheck size={20} /><strong>Verified by design</strong><p>Approved releases. Explicit deployment. Reported verification.</p></div><div className="account"><span className="avatar">AD</span><div><strong>Administrator</strong><small>{runtime.demo ? 'Demo workspace' : 'Secure session'}</small></div>{!runtime.demo && <button className="icon-button" aria-label="Sign out" onClick={signOut} disabled={busy}><LogOut size={18} /></button>}</div></div>
    </aside>
    <div className="main-shell"><header className="topbar"><div className="breadcrumb">Workspace <ChevronRight size={14} /><strong>{page}</strong></div><div className="topbar-right"><span className={`environment ${runtime.demo ? 'demo' : ''}`}><span className="status-dot" />{runtime.demo ? 'DEMO ENVIRONMENT' : 'LIVE ENVIRONMENT'}</span><span className="avatar compact">AD</span>{!runtime.demo && <button className="icon-button mobile-logout" aria-label="Sign out" disabled={busy} onClick={signOut}><LogOut size={18} /></button>}</div></header>
      {runtime.demo && <div className="demo-banner" role="status"><FlaskConical size={17} /><strong>DEMO MODE</strong><span>Fixture data only. All actions are simulated; no cloud requests or device commands.</span></div>}
      <main className="main-content"><div className="page-heading"><div><p className="eyebrow">YOUR EDGE, AT A GLANCE</p><h1>{page === 'Overview' ? 'Workspace overview' : page === 'Firmware' ? 'Firmware releases' : page === 'Deployments' ? 'Deployment activity' : 'Device inventory'}</h1><p>{page === 'Overview' ? 'Monitor your devices. Ship with confidence.' : page === 'Firmware' ? 'A controlled path from binary to approved release.' : page === 'Deployments' ? 'Track every release from dispatch to application verification.' : 'Connectivity and operating state, clearly separated.'}</p></div><button className="secondary" onClick={refresh} disabled={busy || authExpired}><RefreshCw size={16} />Refresh</button></div>
        <div className="sync-line"><span className={`status-dot ${pollError ? 'amber-dot' : ''}`} />{pollError ? 'Updates interrupted · showing last known data' : loaded ? `Last synced ${dateTime(lastSync)} · refreshes every 5s` : 'Loading workspace…'}</div>
        {pollError && <div className="alert error" role="alert">{pollError}</div>}
        {authExpired && <div className="alert"><span>Session expired. Actions are locked.</span><button onClick={() => void runtime.login().catch(() => setNotice('Sign-in could not be started.'))}>Sign in again</button></div>}
        {notice && <div className="alert" role="status"><span>{notice}</span><button className="icon-button" aria-label="Dismiss notification" onClick={() => setNotice('')}><X size={16} /></button></div>}
        {pending && <div className="alert warning" role="alert"><div><strong>{armMessage}</strong><p>Request: <code>{pending.request_id}</code></p></div><button className="secondary" disabled={busy || authExpired} onClick={() => { setSelected(DEVICE); setReleaseId(pending.release_id); setPage('Devices'); setModal('deploy') }}>Review saved request</button></div>}
        {(page === 'Overview' || page === 'Devices') && <>
          <section className="stats-grid" aria-label="Device summary"><Stat label="Gateways" value={loaded ? gateways.length : '—'} icon={<Server size={21} />} online={onlineCount(gateways)} offline={gateways.length - onlineCount(gateways)} /><Stat label="Subdevices" value={loaded ? subdevices.length : '—'} icon={<Cpu size={21} />} online={onlineCount(subdevices)} offline={subdevices.length - onlineCount(subdevices)} /><Stat label="Approved releases" value={loaded ? data.releases.filter(r => r.status === 'approved').length : '—'} icon={<ShieldCheck size={21} />} detail="Ready for deployment" /><Stat label="Verified deployments" value={loaded ? data.deployments.filter(j => j.flash_verified && j.application_health_verified).length : '—'} icon={<Rocket size={21} />} detail="Flash + application confirmed" /></section>
          <div className="device-grid"><section className="panel inventory"><div className="panel-heading"><div><h2>Device fleet <span className="count-pill">{data.devices.length}</span></h2><p>Your connected hierarchy</p></div><Network size={20} /></div><div className="tree" aria-label="Device hierarchy">
            {!loaded && <p className="empty">Device observations will appear here.</p>}
            {gateways.map(gateway => <div key={gateway.device_id}><DeviceButton name={gateway.device_id} kind="Gateway" disabled={busy || Boolean(pending) || authExpired} selected={selected === gateway.device_id} online={connected(gateway, data.devices, now)} onClick={() => setSelected(gateway.device_id)} />{subdevices.filter(sub => sub.parent_id === gateway.device_id).map(sub => <div className="tree-child" key={sub.device_id}><DeviceButton name={sub.device_id} kind="Subdevice" disabled={busy || Boolean(pending) || authExpired} selected={selected === sub.device_id} online={connected(sub, data.devices, now)} onClick={() => setSelected(sub.device_id)} /></div>)}</div>)}
          </div><div className="panel-foot"><Radio size={15} />Connectivity requires observations within 35s.</div></section>
          <section className="panel detail"><div className="panel-heading"><div className="device-title"><span className="device-icon"><Cpu size={25} /></span><div><span className="eyebrow">{device?.kind === 'gateway' ? 'GATEWAY' : 'SUBDEVICE'}</span><h2>{device?.device_id ?? 'Select a device'}</h2></div></div>{device && <Badge tone={connected(device, data.devices, now) ? 'green' : 'neutral'}>{connected(device, data.devices, now) ? 'Online' : 'Offline'}</Badge>}</div>
            {device ? <><dl className="detail-facts"><Fact label="Current firmware" value={device.firmware ? `v${device.firmware}` : 'Not reported'} /><Fact label="Operating state" value={<span className={`operating ${device.state === 'fault' ? 'fault' : ''}`}>{device.state ?? 'Not reported'}</span>} /><Fact label="Last observation" value={dateTime(device.last_seen)} /><Fact label="Update status" value={device.updating ? 'Updating' : 'Not updating'} /></dl><div className="detail-meta"><span>{device.parent_id ? <>Gateway <strong>{device.parent_id}</strong></> : 'Gateway connection'}</span><span>{!connected(device, data.devices, now) ? 'State and firmware are last known values.' : 'State comes from validated device observations.'}</span></div>
              <div className="deploy-box"><div className="deploy-title"><Rocket size={18} /><h3>Deploy firmware</h3>{runtime.demo && <span className="mini-demo">SIMULATED</span>}</div><label htmlFor="release-select">Approved release</label><div className="deploy-controls"><select id="release-select" value={selectedRelease} disabled={busy || Boolean(pending) || authExpired} onChange={event => setReleaseId(event.target.value)}><option value="">Choose a release</option>{data.releases.filter(r => r.status === 'approved').map(r => <option key={r.release_id} value={r.release_id}>v{r.version} · {(r.size_bytes / 1024).toFixed(1)} KiB</option>)}</select><button disabled={busy || Boolean(blocked)} onClick={() => setModal('deploy')}><Rocket size={16} />{pending ? 'Review deployment' : runtime.demo ? 'Simulate deploy' : 'Review deploy'}</button></div><p className="hint">{blocked ?? (pending ? 'Keep this request ID. Arming and sending are separate actions.' : 'Review current and approved versions, arm for 120 seconds, then confirm and send.')}</p></div>
            </> : <p className="empty">Select a gateway or subdevice to inspect it.</p>}
          </section></div>
        </>}
        {(page === 'Overview' || page === 'Firmware') && <section className="panel releases"><div className="panel-heading"><div><h2>Firmware library <span className="count-pill">{data.releases.length}</span></h2><p>Immutable releases for STM32G0B1</p></div><button onClick={() => setModal('upload')} disabled={busy || authExpired}><Plus size={17} />Upload firmware</button></div><div className="table-scroll"><table><thead><tr><th>Release</th><th>Approval</th><th>Size</th><th>SHA-256</th><th>Created</th><th><span className="sr-only">Actions</span></th></tr></thead><tbody>{[...data.releases].sort((a, b) => b.created_at.localeCompare(a.created_at)).map(release => <tr key={release.release_id}><td><div className="release-version"><span className="file-icon"><Box size={18} /></span><div><strong>v{release.version}</strong><small>{release.target}</small></div></div></td><td><Badge tone={release.status === 'approved' ? 'green' : 'amber'}>{release.status === 'approved' ? 'Approved' : 'Awaiting approval'}</Badge></td><td>{(release.size_bytes / 1024).toFixed(1)} KiB</td><td><code title={release.sha256}>{release.sha256.slice(0, 8)}…{release.sha256.slice(-6)}</code></td><td>{dateTime(release.created_at)}</td><td>{release.status === 'uploaded' ? <button className="secondary small" disabled={busy || authExpired || Boolean(pollError)} onClick={() => void action(signal => runtime.api.approve(release.release_id, signal), runtime.demo ? 'Demo release approved locally.' : 'Release integrity validated and approved.')}><ShieldCheck size={15} />Approve</button> : <button className="text-button" disabled={busy || Boolean(pending) || authExpired} onClick={() => { setReleaseId(release.release_id); setSelected(DEVICE); setPage('Devices') }}>Select <ArrowRight size={15} /></button>}</td></tr>)}</tbody></table></div>{loaded && !data.releases.length && <p className="empty">No firmware releases yet. Upload a binary to get started.</p>}<div className="panel-foot"><ShieldCheck size={15} />Approval revalidates the stored binary before signing. {runtime.demo && 'Demo hashes are fixtures.'}</div></section>}
        {(page === 'Overview' || page === 'Deployments') && <section className="panel"><div className="panel-heading"><div><h2>{page === 'Overview' ? 'Recent deployments' : 'Deployment history'}</h2><p>Reported stages, not an estimate of time remaining</p></div>{page === 'Overview' && <button className="text-button" onClick={() => setPage('Deployments')}>View all <ArrowRight size={15} /></button>}</div>{!sortedJobs.length ? <div className="empty"><History size={28} /><h3>No deployments yet</h3><p>Choose an approved release from a fresh subdevice to begin.</p></div> : <div className="job-list">{(page === 'Overview' ? sortedJobs.slice(0, 3) : sortedJobs).map(job => <Job key={job.command_id} job={job} demo={runtime.demo} />)}</div>}<div className="panel-foot"><CircleHelp size={15} />A cloud status alone is not hardware proof. Verification flags are reported by the gateway.</div></section>}
        <footer className="footer"><span><Cloud size={14} />{runtime.demo ? 'Isolated demo · no network access' : 'Authenticated edge operations'}</span><span>cloud demo / Lab MVP</span></footer>
      </main>
    </div>
    {modal === 'upload' && <UploadDialog demo={runtime.demo} onClose={() => setModal(null)} onUpload={async body => {
      setModal(null)
      await action(signal => runtime.api.upload(body, signal), runtime.demo ? 'Demo binary added locally. Approval is the next step.' : 'Binary uploaded. Review and approve the release before deployment.')
    }} />}
    {modal === 'deploy' && <Modal title={runtime.demo ? 'Review simulated deployment' : 'Review deployment'} onClose={() => setModal(null)}>
      <div className="confirmation-icon"><Rocket size={28} /></div>
      <p>{runtime.demo ? 'Demo fixtures only; no cloud requests or device commands. ' : ''}Review the target and versions before arming. An arm lasts 120 seconds and does not publish or install. Sending requires a separate confirmation; there are no POST retries.</p>
      <dl className="confirm-facts"><Fact label="Target" value={DEVICE} /><Fact label="Gateway" value={GATEWAY} /><Fact label="Current version" value={device?.firmware ?? 'Unknown'} /><Fact label="Approved version" value={pending?.release_id ?? chosenRelease?.version ?? 'Unknown'} /></dl>
      {pending && <><p className="request-id">Request <code>{pending.request_id}</code></p><p role="status">{armMessage}</p></>}
      {pending?.stage === 'armed' && !canAbandon && pending.arm.from_version !== device?.firmware && <p role="alert">Current firmware changed since arming. Sending is locked; wait for expiry and review again.</p>}
      {blocked && <p className="alert error" role="alert">{blocked}</p>}
      <div className="modal-actions"><button className="secondary" onClick={() => setModal(null)}>Cancel</button>
        {!pending && <button disabled={Boolean(blocked) || busy} onClick={armDeployment}>Arm this deployment</button>}
        {canAbandon && <button disabled={busy || authExpired} onClick={abandonArm}>Abandon unused arm and review again</button>}
        <button disabled={Boolean(blocked) || busy || !canSend} onClick={confirmDeploy}><Rocket size={16} />Confirm and send</button>
      </div>
    </Modal>}
  </div>
}

function Stat({ label, value, icon, online, offline, detail }: { label: string; value: number | string; icon: ReactNode; online?: number; offline?: number; detail?: string }) {
  return <article className="stat"><div className="stat-label">{label}<span>{icon}</span></div><strong className="stat-value">{value}</strong>{online !== undefined ? <div className="stat-status"><span><i className="status-dot" />{online} online</span><span><i className="status-dot offline-dot" />{offline} offline</span></div> : <p>{detail}</p>}</article>
}
function DeviceButton({ name, kind, selected, online, onClick, disabled }: { name: string; kind: string; selected: boolean; online: boolean; onClick: () => void; disabled: boolean }) {
  return <button disabled={disabled} className={`device-row ${selected ? 'selected' : ''}`} onClick={onClick} aria-pressed={selected}>{kind === 'Gateway' ? <Server size={20} /> : <Cpu size={20} />}<span><strong>{name}</strong><small>{kind}</small></span><span className={`connectivity-dot ${online ? 'online' : ''}`} aria-label={online ? 'Online' : 'Offline'} /><ChevronRight size={15} /></button>
}
function Fact({ label, value }: { label: string; value: ReactNode }) { return <div className="fact"><dt>{label}</dt><dd>{value}</dd></div> }
function Job({ job, demo }: { job: Deployment; demo: boolean }) {
  const verified = job.flash_verified && job.application_health_verified
  return <article className="job"><div className="job-top"><span className={`job-icon ${verified ? 'verified' : ''}`}>{verified ? <Check size={20} /> : <ArrowDownToLine size={20} />}</span><div><h3>v{job.from_version}<ArrowRight size={14} />v{job.version}{demo && <span className="mini-demo">DEMO</span>}</h3><p>{job.device_id} <span>· {dateTime(job.created_at)}</span></p></div><Badge tone={verified ? 'green' : job.status === 'error' || job.phase === 'dispatch_unknown' ? 'amber' : 'blue'}>{phaseLabel(job)}</Badge></div><div className="progress" role="progressbar" aria-label={`Reported progress for ${job.version}`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={progress(job)} aria-valuetext={phaseLabel(job)}><span style={{ width: `${progress(job)}%` }} /></div><div className="job-bottom"><span className={job.flash_verified ? 'confirmed' : ''}><ShieldCheck size={14} />Flash {job.flash_verified ? 'verified' : 'not verified'}</span><span className={job.application_health_verified ? 'confirmed' : ''}><Activity size={14} />Application {job.application_health_verified ? 'verified' : 'not verified'}</span><span>Updated {dateTime(job.updated_at)}</span></div>{job.error && <p className="job-warning">{job.phase === 'dispatch_unknown' ? 'Dispatch outcome is unknown. Do not start another deployment; investigation is required.' : 'The update did not complete successfully. Review the deployment before any further action.'}</p>}<details><summary>Request details</summary><code>{job.command_id}</code></details></article>
}
function Modal({ title, children, onClose }: { title: string; children: ReactNode; onClose: () => void }) {
  const ref = useRef<HTMLDialogElement>(null)
  useEffect(() => {
    const dialog = ref.current!
    const previous = document.activeElement as HTMLElement | null
    dialog.showModal()
    return () => { dialog.close(); previous?.focus() }
  }, [])
  return <dialog ref={ref} className="modal" aria-labelledby="modal-title" onCancel={event => { event.preventDefault(); onClose() }}><div className="modal-heading"><h2 id="modal-title">{title}</h2><button className="icon-button" aria-label="Close dialog" onClick={onClose}><X size={19} /></button></div>{children}</dialog>
}
export function UploadDialog({ demo, onClose, onUpload }: { demo: boolean; onClose: () => void; onUpload: (body: Awaited<ReturnType<typeof prepareUpload>>) => Promise<void> }) {
  const [version, setVersion] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [error, setError] = useState('')
  const [checking, setChecking] = useState(false)
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])
  async function submit(event: FormEvent) {
    event.preventDefault()
    const invalid = validateFile(file, version)
    if (invalid) { setError(invalid); return }
    setChecking(true); setError('')
    let body: Awaited<ReturnType<typeof prepareUpload>>
    try { body = await prepareUpload(file!, version) }
    catch (error) { setError(error instanceof Error && /^(The binary has|The embedded firmware)/.test(error.message) ? error.message : 'The binary could not be read. Choose a valid file.'); setChecking(false); return }
    if (mounted.current) await onUpload(body)
  }
  return <Modal title={demo ? 'Upload demo firmware' : 'Upload firmware'} onClose={onClose}><p>Upload an immutable binary release. Approval is a separate step; uploading never deploys firmware.</p>{demo && <p className="demo-inline">DEMO · file stays in this browser session.</p>}<form onSubmit={event => void submit(event)}><label htmlFor="version">Release version</label><input id="version" placeholder="e.g. 0.5.0" value={version} onChange={e => setVersion(e.target.value)} autoComplete="off" disabled={checking} /><p className="hint">Numeric major.minor.patch, matching the embedded version.</p><label className="upload-zone" htmlFor="binary"><FileUp size={32} /><strong>{file?.name ?? 'Choose firmware binary'}</strong><span>{file ? `${file.size.toLocaleString()} bytes` : '.bin format · 8 bytes to 20 KiB'}</span><input id="binary" aria-label="Firmware binary" type="file" accept=".bin,application/octet-stream" disabled={checking} onChange={e => setFile(e.target.files?.[0] ?? null)} /></label><p className="hint">Vector entries and embedded version are checked locally. The server performs authoritative validation.</p>{error && <p className="alert error" role="alert">{error}</p>}<div className="upload-warning"><CircleHelp size={17} /><span>Versions cannot be overwritten. After an uncertain upload, refresh the library before using a new version; do not retry blindly.</span></div><div className="modal-actions"><button type="button" className="secondary" onClick={onClose}>Cancel</button><button type="submit" disabled={checking}><FileUp size={16} />{checking ? 'Validating…' : demo ? 'Add demo release' : 'Upload binary'}</button></div></form></Modal>
}
