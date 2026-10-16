import React, { useEffect, useMemo, useState } from 'react';
import { Activity, ArrowDownRight, ArrowUpRight, Bell, Bookmark, Check, ChevronDown, CircleHelp, Clock3, ExternalLink, FileText, Filter, Globe2, History, Layers3, LoaderCircle, MapPin, Radio, RefreshCw, Search, Settings2, ShieldCheck, Sparkles, Target, Trash2, Upload, X } from 'lucide-react';

const API = import.meta.env.VITE_API_URL ?? '';
const dateLabel = (date) => date ? new Date(`${date}T00:00:00`).toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: 'numeric' }) : 'Not verified';
const verificationLabel = (status) => ({ demo: 'Demo data', page_fetched: 'Page fetched', search_result: 'Search result only' }[status] || 'Not verified');
const registrationStatusLabel = (status) => ({ open: 'Open', closed: 'Closed', closing_today: 'Deadline today', unknown: 'Status unknown' }[status] || 'Status unknown');
const sourceInitial = (source = '?') => source.replace(/^www\./, '').charAt(0).toUpperCase();

const outcomeLabel = { new: 'New', updated: 'Updated', seen: 'Seen before' };
const statusLabelShort = { open: 'Open', closed: 'Closed', closing_today: 'Deadline today', unknown: 'Unknown' };
const platformOf = (e) => e.platform_label || e.source;
const fmtWhen = (iso) => new Date(iso).toLocaleString('en-IN', { day: 'numeric', month: 'short', hour: 'numeric', minute: '2-digit' });
const fmtDay = (d) => (d ? new Date(`${d}T00:00:00`).toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: 'numeric' }) : 'Not verified');
const isHttp = (u) => /^https?:\/\//i.test(u || '');

function ScanHistory({ api, refreshKey, onBanner }) {
  const [scans, setScans] = useState(null);
  const [selectedId, setSelectedId] = useState(null);
  const [detail, setDetail] = useState(null);
  const [platformFilter, setPlatformFilter] = useState('all');
  const [outcomeFilter, setOutcomeFilter] = useState('all');
  const [error, setError] = useState('');

  useEffect(() => {
    let alive = true;
    api('/api/scans').then(list => {
      if (!alive) return;
      setScans(list); setSelectedId(list[0]?.id ?? null); setError('');
    }).catch(e => alive && setError(e.message));
    return () => { alive = false; };
  }, [refreshKey]);

  useEffect(() => {
    if (selectedId == null) { setDetail(null); return undefined; }
    let alive = true;
    setPlatformFilter('all'); setOutcomeFilter('all'); setDetail(null);
    api(`/api/scans/${selectedId}`).then(d => alive && setDetail(d)).catch(e => alive && setError(e.message));
    return () => { alive = false; };
  }, [selectedId]);

  async function clearHistory() {
    if (!window.confirm('Clear scan history? Your feed, saved and dismissed events are not affected.')) return;
    try { await api('/api/scans', { method: 'DELETE' }); setScans([]); setSelectedId(null); setDetail(null); onBanner('Scan history cleared.'); }
    catch (e) { onBanner(e.message); }
  }

  if (error && !scans) return <div className="empty-state"><h3>Could not load scan history</h3><p>{error}</p></div>;
  if (!scans) return <div className="empty-state"><LoaderCircle className="spin" size={22}/><h3>Loading scan history…</h3></div>;
  if (!scans.length) return <div className="empty-state"><div className="empty-icon"><History size={22}/></div><h3>No scans recorded yet</h3><p>Press Scan web. Every scan, the platforms it searched and the listings it found will be kept here.</p></div>;

  const events = detail?.events || [];
  const platformNames = [...new Set(events.map(e => e.platform_label))];
  const shown = events.filter(e => (platformFilter === 'all' || e.platform_label === platformFilter) && (outcomeFilter === 'all' || e.outcome === outcomeFilter));
  const platformState = (p) => (p.errors?.length ? ['Search error', 'warn'] : p.results === 0 ? ['No results', 'none'] : p.kept ? ['Listings kept', 'ok'] : ['Found, none kept', 'none']);

  return (
    <section className="history">
      <div className="section-heading"><div><div className="section-kicker">SCAN HISTORY</div><h2>Every scan, with its data</h2><p>Each row is a listing the scan saw, including ones you already had.</p></div>
        <div className="heading-actions"><button className="secondary-button" onClick={clearHistory}>Clear history</button></div></div>
      <div className="history-layout">
        <ol className="scan-list" aria-label="Past scans">{scans.map(s => (
          <li key={s.id}><button className={`scan-item ${s.id === selectedId ? 'active' : ''}`} onClick={() => setSelectedId(s.id)}>
            <div className="scan-item-top"><strong>{fmtWhen(s.scanned_at)}</strong><span className={`mode-pill ${s.mode}`}>{s.mode === 'demo' ? 'Demo' : 'Live'}</span></div>
            <div className="scan-item-stats"><span>{s.kept} listings</span><span>{s.new_count} new</span><span>{s.updated_count} updated</span>{s.mode === 'live' && <span>{s.platform_count} platforms</span>}</div>
            {s.errors.length > 0 && <div className="scan-item-warn">{s.errors.length} search issue{s.errors.length > 1 ? 's' : ''}</div>}
          </button></li>))}
        </ol>
        <div className="scan-detail">
          {!detail ? <div className="empty-state"><LoaderCircle className="spin" size={20}/><p>Loading this scan…</p></div> : <>
            <div className="scan-summary">
              {[['Listings seen', detail.kept], ['New', detail.new_count], ['Updated', detail.updated_count], ['Searches run', detail.queries], ['Pages fetched', detail.fetched], ['Took', `${Math.round(detail.duration_seconds)}s`]].map(([label, value]) => <div key={label}><span>{label}</span><strong>{value}</strong></div>)}
            </div>
            <p className="scan-note">{detail.provider === 'gemini' ? `Extraction: Gemini${detail.gemini_model ? ` (${detail.gemini_model})` : ''}.` : detail.provider === 'demo' ? 'Demo data, not from the live web.' : detail.provider === 'tinyfish_agent' ? 'Extraction: paid TinyFish Agent.' : 'Extraction: rule-based.'} {detail.note}</p>
            {detail.mode === 'live' && detail.platforms.length > 0 && <div className="table-wrap"><table className="history-table">
              <thead><tr><th>Platform</th><th>Searches</th><th>Results</th><th>Shortlisted</th><th>Kept</th><th>New</th><th>Status</th></tr></thead>
              <tbody>{detail.platforms.map(p => { const [label, tone] = platformState(p); return <tr key={p.key}><td><strong>{p.label}</strong></td><td>{p.queries}</td><td>{p.results}</td><td>{p.shortlisted}</td><td>{p.kept}</td><td>{p.new}</td><td><span className={`state-pill ${tone}`} title={(p.errors || []).join(' ')}>{label}</span></td></tr>; })}</tbody>
            </table></div>}
            {detail.errors.length > 0 && <div className="scan-errors"><strong>Search issues</strong><ul>{detail.errors.map(er => <li key={er}>{er}</li>)}</ul></div>}
            <div className="history-filters">
              <div className="platform-chips" role="group" aria-label="Filter scanned listings by platform">
                <button className={`chip-button ${platformFilter === 'all' ? 'active' : ''}`} onClick={() => setPlatformFilter('all')}>All platforms <b>{events.length}</b></button>
                {platformNames.map(n => <button key={n} className={`chip-button ${platformFilter === n ? 'active' : ''}`} onClick={() => setPlatformFilter(n)}>{n} <b>{events.filter(e => e.platform_label === n).length}</b></button>)}
              </div>
              <div className="platform-chips" role="group" aria-label="Filter scanned listings by outcome">
                {['all', 'new', 'updated', 'seen'].map(o => <button key={o} className={`chip-button ${outcomeFilter === o ? 'active' : ''}`} onClick={() => setOutcomeFilter(o)}>{o === 'all' ? 'Any outcome' : outcomeLabel[o]}</button>)}
              </div>
            </div>
            {shown.length === 0 ? <div className="empty-state"><p>No listings match these filters.</p></div> : <div className="table-wrap"><table className="history-table">
              <thead><tr><th>Listing</th><th>Platform</th><th>Deadline</th><th>Status</th><th>Format</th><th>Match</th><th>This scan</th></tr></thead>
              <tbody>{shown.map(e => <tr key={e.id}>
                <td className="listing-cell">{isHttp(e.url) && !e.is_demo ? <a href={e.url} target="_blank" rel="noopener noreferrer">{e.title}</a> : <span>{e.title}</span>}<div className="tag-row">{(e.tags || []).slice(0, 3).map(t => <span className="tag" key={t}>{t}</span>)}</div></td>
                <td>{e.platform_label}</td><td>{fmtDay(e.deadline)}</td>
                <td><span className={`registration-pill status-${e.registration_status}`}>{statusLabelShort[e.registration_status] || 'Unknown'}</span></td>
                <td>{e.format}</td><td>{e.match_score}</td>
                <td><span className={`outcome-pill ${e.outcome}`}>{outcomeLabel[e.outcome] || e.outcome}</span></td>
              </tr>)}</tbody>
            </table></div>}
          </>}
        </div>
      </div>
    </section>
  );
}

export default function App() {
  const [events, setEvents] = useState([]);
  const [profile, setProfile] = useState({ skills: ['AI/ML', 'Python', 'Web Development'], interests: ['Artificial Intelligence', 'Open Innovation'], level: 'Student', location: 'India', prefer_online: true });
  const [apiMode, setApiMode] = useState('checking');
  const [agentStatus, setAgentStatus] = useState('Checking extraction setup…');
  const [agentBudget, setAgentBudget] = useState({ maxPages: 2, budgetUsd: 4.8, confirmedUsd: 0, reservedUsd: 0 });
  const [paidAgent, setPaidAgent] = useState(false);
  const [provider, setProvider] = useState('rules');
  const [busy, setBusy] = useState(false);
  const [banner, setBanner] = useState('');
  const [activeTab, setActiveTab] = useState('Discover');
  const [selected, setSelected] = useState(null);
  const [popup, setPopup] = useState(null);
  const [search, setSearch] = useState('');
  const [showProfile, setShowProfile] = useState(false);
  const [showAll, setShowAll] = useState(false);
  const [platform, setPlatform] = useState('all');
  const [resume, setResume] = useState(null);
  const [resumeBusy, setResumeBusy] = useState(false);
  const [resumeError, setResumeError] = useState('');
  const [historyKey, setHistoryKey] = useState(0);

  async function api(path, options = {}) {
    const res = await fetch(`${API}${path}`, { headers: { 'Content-Type': 'application/json', ...(options.headers || {}) }, ...options });
    const payload = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(payload.detail || `Request failed (${res.status})`);
    return payload;
  }
  async function refresh() {
    try {
      const [health, data, p, r] = await Promise.all([api('/api/health'), api('/api/events'), api('/api/profile'), api('/api/resume').catch(() => ({ present: false }))]);
      setApiMode(health.mode); setEvents(data); setProfile(p); setResume(r.present ? r : null);
      if (health.mode !== 'live' && health.env) setBanner(prev => prev || `TinyFish key not found. HackScout looked in ${health.env.file} and ${health.env.status}. Fix it, save, then stop the backend (Ctrl+C) and start it again.`);
      const confirmedUsd = Number(health.tinyfish_agent_reported_cost_usd ?? health.tinyfish_agent_estimated_usd_used ?? 0);
      const reservedUsd = Number(health.tinyfish_agent_unconfirmed_reserve_usd || 0);
      const budgetUsd = Number(health.tinyfish_agent_estimated_usd_budget || 4.8);
      const maxPages = Number(health.tinyfish_agent_max_pages_per_scan || 1);
      setAgentBudget({ maxPages, budgetUsd, confirmedUsd, reservedUsd });
      const activeProvider = health.event_extraction_provider || 'rules';
      setProvider(activeProvider);
      setPaidAgent(Boolean(health.paid_tinyfish_agent_enabled));
      if (activeProvider === 'tinyfish_agent') {
        setAgentStatus(health.tinyfish_agent_configured
          ? `Paid Agent $${confirmedUsd.toFixed(2)} reported · $${reservedUsd.toFixed(2)} unconfirmed reserve / $${budgetUsd.toFixed(2)}`
          : `Paid Agent unavailable · $${confirmedUsd.toFixed(2)} reported · $${reservedUsd.toFixed(2)} reserved`);
      } else if (activeProvider === 'gemini') {
        setAgentStatus('Gemini extraction · no TinyFish Agent charges');
      } else {
        setAgentStatus('Rule-based extraction · no Agent charges (add GEMINI_API_KEY for better results)');
      }
    } catch {
      setApiMode('offline'); setEvents([]); setBanner('Backend is not connected. Start the backend to use HackScout. Demo listings are not mixed into the normal feed.');
    }
  }
  useEffect(() => { refresh(); }, []);

  async function scan(demo = false) {
    if (!demo && apiMode === 'live' && paidAgent) {
      const proceed = window.confirm(`Live scan may spend TinyFish Agent wallet steps. This scan can inspect up to ${agentBudget.maxPages} event page(s). Usage reported to HackScout: $${agentBudget.confirmedUsd.toFixed(2)}; unconfirmed safety reserve: $${agentBudget.reservedUsd.toFixed(2)}; app-level budget: $${agentBudget.budgetUsd.toFixed(2)}. This is not a provider-enforced wallet limit and cannot account for TinyFish usage outside HackScout. Check your TinyFish wallet before continuing. Continue?`);
      if (!proceed) return;
    }
    setBusy(true); setBanner(demo ? 'Loading clearly labelled sample data…' : (provider === 'gemini' ? 'Searching and fetching pages with TinyFish, then reading them with Gemini…' : provider === 'tinyfish_agent' ? `Searching with TinyFish and checking up to ${agentBudget.maxPages} candidate event page(s) with the paid TinyFish Agent. Confirmed usage and unconfirmed reservations are tracked separately.` : 'Searching and fetching pages with TinyFish, then extracting details with rules…'));
    try {
      const data = await api('/api/scan', { method: 'POST', body: JSON.stringify({ use_demo: demo }) });
      setEvents(data.events || []);
      setApiMode(data.mode === 'demo' ? 'demo' : 'live');
      setBanner(data.note || `${data.new_count} new listings · ${data.updated_count} updates`);
      // A "new match" popup should only represent a genuinely new result from this scan.
      // Never reopen the first old feed item just because the scan had no new match.
      setPopup(data.next_match || null);
      setHistoryKey(k => k + 1);
    } catch (error) { setBanner(error.message); }
    finally { setBusy(false); }
  }
  async function eventAction(event, action) {
    try { await api(`/api/events/${event.id}?action=${action}`, { method: 'PATCH' }); }
    catch (e) { if (event.id > 0) setBanner(e.message); }
    const isSaved = action === 'save' ? true : action === 'unsave' ? false : event.is_saved;
    const isDismissed = action === 'dismiss' ? true : action === 'restore' ? false : event.is_dismissed;
    setEvents(prev => prev.map(e => e.id === event.id ? { ...e, is_saved: isSaved, is_dismissed: isDismissed } : e));
    setSelected(prev => prev?.id === event.id ? { ...prev, is_saved: isSaved, is_dismissed: isDismissed } : prev);
    if (action === 'save' || action === 'unsave') {
      setPopup(prev => prev?.id === event.id ? { ...prev, is_saved: isSaved, is_dismissed: isDismissed } : prev);
    }
    if (action === 'save') setBanner('Saved to your shortlist.');
    if (action === 'dismiss') { setPopup(null); setBanner('Popup dismissed. The listing remains in your feed.'); }
    if (action === 'restore') setBanner('Listing restored.');
  }
  async function uploadResume(file) {
    if (!file) return;
    setResumeError('');
    if (!/\.pdf$/i.test(file.name) && file.type !== 'application/pdf') { setResumeError('Please choose a PDF file.'); return; }
    if (file.size > 5 * 1024 * 1024) { setResumeError('That PDF is larger than 5 MB.'); return; }
    setResumeBusy(true);
    try {
      const r = await api('/api/resume', { method: 'POST', headers: { 'Content-Type': 'application/pdf', 'X-Filename': encodeURIComponent(file.name) }, body: file });
      setResume(r);
      setEvents(await api('/api/events'));
      setBanner(`Resume read: found ${r.skill_count} skill${r.skill_count === 1 ? '' : 's'}${r.interests.length ? ` and ${r.interests.length} interest area${r.interests.length === 1 ? '' : 's'}` : ''}. Matches were re-ranked. Run Scan web to search for events that fit it.`);
    } catch (e) { setResumeError(e.message); }
    finally { setResumeBusy(false); }
  }
  async function removeResume() {
    setResumeError('');
    try {
      await api('/api/resume', { method: 'DELETE' });
      setResume(null);
      setEvents(await api('/api/events'));
      setBanner('Resume removed. Matching now uses your profile only.');
    } catch (e) { setResumeError(e.message); }
  }
  async function saveProfile() {
    try { const updated = await api('/api/profile', { method: 'PUT', body: JSON.stringify(profile) }); setProfile(updated); setShowProfile(false); await refresh(); setBanner('Profile updated. Matching scores refreshed.'); }
    catch (e) { setBanner(e.message); }
  }

  const visibleEvents = useMemo(() => events.filter(e => activeTab === 'Saved' ? e.is_saved : activeTab === 'Dismissed' ? e.is_dismissed : !e.is_dismissed && e.registration_status !== 'closed').filter(e => `${e.title} ${platformOf(e)} ${e.source} ${e.tags?.join(' ')} ${e.snippet} ${e.registration_status || ''}`.toLowerCase().includes(search.toLowerCase())).filter(e => platform === 'all' || platformOf(e) === platform), [events, activeTab, search, platform]);
  const platformCounts = useMemo(() => {
    const counts = {};
    events.filter(e => activeTab === 'Saved' ? e.is_saved : activeTab === 'Dismissed' ? e.is_dismissed : !e.is_dismissed && e.registration_status !== 'closed')
      .forEach(e => { const name = platformOf(e); counts[name] = (counts[name] || 0) + 1; });
    return Object.entries(counts).sort((a, b) => b[1] - a[1]);
  }, [events, activeTab]);
  const displayed = showAll ? visibleEvents : visibleEvents.slice(0, 10);
  const counts = { all: events.filter(e => !e.is_dismissed && e.registration_status !== 'closed').length, saved: events.filter(e => e.is_saved).length, new: events.filter(e => !e.is_saved && !e.is_dismissed && e.registration_status !== 'closed').length };

  return <div className="app-shell">
    <aside className="sidebar">
      <div className="brand"><div className="brand-mark"><Radio size={21} strokeWidth={2.5}/></div><span>hack<span>scout</span><i>.</i></span></div>
      <div className="workspace-label">WORKSPACE</div>
      <nav className="nav-list">
        {[['Discover', Search], ['Saved', Bookmark], ['Dismissed', X], ['Scan history', History]].map(([label, Icon]) => <button className={`nav-item ${activeTab === label ? 'active' : ''}`} key={label} onClick={() => setActiveTab(label)}><Icon size={17}/><span>{label}</span>{label === 'Saved' && counts.saved > 0 && <b>{counts.saved}</b>}</button>)}
      </nav>
      <div className="sidebar-spacer" />
      <div className="profile-mini"><div className="avatar">HS</div><div className="profile-copy"><strong>Your scout profile</strong><span>{profile.level} · {profile.location}{resume ? ' · Resume added' : ''}</span></div><button className="icon-button" onClick={() => setShowProfile(true)} aria-label="Edit profile"><Settings2 size={17}/></button></div>
      <div className="sidebar-foot"><span className={`status-dot ${apiMode === 'live' ? 'green' : apiMode === 'offline' ? 'red' : ''}`}/>{apiMode === 'live' ? 'Live discovery ready' : apiMode === 'demo' ? 'Demo mode' : apiMode === 'offline' ? 'Backend offline' : 'Checking connection'}<span className="version">v0.1</span></div>
    </aside>

    <main className="main-content">
      <header className="topbar"><div className="breadcrumbs">Workspace <span>/</span> <strong>{activeTab}</strong></div><div className="top-actions"><span className="today-label"><Activity size={14}/> Your opportunity radar</span><button className="avatar avatar-small" onClick={() => setShowProfile(true)} title="Edit profile">HS</button></div></header>
      <section className="page-wrap">
        <div className="hero-row"><div><div className="eyebrow"><span className="eyebrow-line"/> PERSONAL HACKATHON RADAR</div><h1>{activeTab === 'Discover' ? <>Find your next<br/><span>big build.</span></> : activeTab === 'Saved' ? <>Your <span>shortlist.</span></> : activeTab === 'Scan history' ? <>Every scan,<br/><span>on record.</span></> : <>Quietly <span>dismissed.</span></>}</h1><p className="hero-sub">A radar for opportunities that fit your skills, your goals, and your next idea.</p></div><div className="hero-graphic"><div className="orbit orbit-one"/><div className="orbit orbit-two"/><div className="orbit orbit-three"/><div className="radar-dot dot-a"/><div className="radar-dot dot-b"/><div className="radar-dot dot-c"/><div className="radar-core"><Target size={29}/></div><div className="radar-sweep"/></div></div>
        {banner && <div className={`notice ${banner.toLowerCase().includes('not connected') || banner.toLowerCase().includes('not configured') || banner.toLowerCase().includes('rate limit') ? 'notice-warn' : ''}`}><CircleHelp size={16}/><span>{banner}</span><button onClick={() => setBanner('')} aria-label="Dismiss message"><X size={15}/></button></div>}
        {activeTab === 'Scan history' ? <ScanHistory api={api} refreshKey={historyKey} onBanner={setBanner} /> : <>
        <div className="stats-grid"><div className="stat-card"><div className="stat-icon green-tint"><Layers3 size={17}/></div><span className="stat-label">IN YOUR RADAR</span><strong>{counts.all.toString().padStart(2, '0')}</strong><small>matching opportunities</small></div><div className="stat-card"><div className="stat-icon blue-tint"><Bookmark size={17}/></div><span className="stat-label">SHORTLISTED</span><strong>{counts.saved.toString().padStart(2, '0')}</strong><small>saved for later</small></div><div className="stat-card"><div className="stat-icon amber-tint"><Sparkles size={17}/></div><span className="stat-label">READY TO EXPLORE</span><strong>{counts.new.toString().padStart(2, '0')}</strong><small>not dismissed</small></div><div className="stat-card scan-stat"><div className="stat-label">DISCOVERY ENGINE</div><div className="engine-status"><span className={`status-dot ${apiMode === 'live' ? 'green' : ''}`}/><strong>{apiMode === 'live' ? 'TinyFish connected' : apiMode === 'demo' ? 'Demo scan complete' : 'Waiting to scan'}</strong></div><small>{apiMode === 'live' ? `TinyFish Search + Fetch · ${agentStatus}` : 'Connect a key for live results'}</small><button className="text-button" onClick={() => scan(false)} disabled={busy}><RefreshCw size={13} className={busy ? 'spin' : ''}/> {busy ? 'Scanning…' : 'Run live scan'} <ArrowUpRight size={13}/></button></div></div>

        <div className="section-heading"><div><div className="section-kicker">YOUR MATCH FEED</div><h2>{activeTab === 'Discover' ? 'Opportunities worth a look' : activeTab === 'Saved' ? 'Kept on your radar' : 'Out of your radar'}</h2><p>Ranked against your skills and interests. Always verify on the original page.</p></div><div className="heading-actions"><button className="secondary-button" onClick={() => scan(true)} disabled={busy}><Sparkles size={15}/> Try demo scan</button><button className="primary-button" onClick={() => scan(false)} disabled={busy}><RefreshCw size={15} className={busy ? 'spin' : ''}/>{busy ? 'Scanning' : 'Scan web'} <ArrowUpRight size={15}/></button></div></div>
        <div className="toolbar"><div className="search-box"><Search size={16}/><input placeholder="Search events, skills, platforms…" value={search} onChange={e => setSearch(e.target.value)}/><kbd>⌘ K</kbd></div><button className="filter-button" onClick={() => setShowProfile(v => !v)}><Filter size={15}/> Match profile <ChevronDown size={14}/></button><span className="result-count">{visibleEvents.length} results</span></div>
        {platformCounts.length > 1 && <div className="platform-chips" role="group" aria-label="Filter by platform"><button className={`chip-button ${platform === 'all' ? 'active' : ''}`} onClick={() => setPlatform('all')}>All platforms <b>{platformCounts.reduce((n, [, c]) => n + c, 0)}</b></button>{platformCounts.map(([name, count]) => <button key={name} className={`chip-button ${platform === name ? 'active' : ''}`} onClick={() => setPlatform(name)}>{name} <b>{count}</b></button>)}</div>}

        <div className="event-list">{displayed.map((event, i) => <article className={`event-card ${selected?.id === event.id ? 'selected' : ''}`} key={`${event.id}-${event.url}`} onClick={() => setSelected(event)}>
          <div className={`event-logo logo-${i % 4}`}>{sourceInitial(platformOf(event))}</div><div className="event-main"><div className="event-meta"><span>{platformOf(event)}</span><span className="meta-dot">·</span><span>{event.is_demo ? 'Illustrative sample' : event.verification === 'page_fetched' ? 'Page retrieved' : 'Search discovery'}</span></div><h3>{event.title}</h3><p>{event.snippet}</p>{event.match_reasons?.length > 0 && <div className="match-why"><Sparkles size={12}/> {event.match_reasons[0]}</div>}<div className="tag-row">{(event.tags || []).slice(0, 4).map(tag => <span className="tag" key={tag}>{tag}</span>)}<span className={`format-tag ${event.format === 'Online' ? 'online' : ''}`}><Globe2 size={11}/>{event.format || 'Format unknown'}</span><span className={`registration-pill status-${event.registration_status || 'unknown'}`}>{registrationStatusLabel(event.registration_status)}</span></div></div>
          <div className="event-side"><div className="match-score"><span className="score-ring" style={{ '--score': `${event.match_score || 0}%` }}><span>{event.match_score || 0}</span></span><span className="match-caption">MATCH</span></div><div className="deadline"><span>{event.deadline ? 'REGISTRATION DEADLINE' : event.submission_deadline ? 'SUBMISSION DEADLINE' : 'DEADLINE'}</span><strong className={!(event.deadline || event.submission_deadline) ? 'muted' : ''}>{dateLabel(event.deadline || event.submission_deadline)}</strong></div><button className={`save-button ${event.is_saved ? 'saved' : ''}`} aria-label={event.is_saved ? 'Unsave event' : 'Save event'} onClick={e => { e.stopPropagation(); eventAction(event, event.is_saved ? 'unsave' : 'save'); }}>{event.is_saved ? <Check size={16}/> : <Bookmark size={16}/>}</button></div>
          <button className="card-open" onClick={e => { e.stopPropagation(); setPopup(event); }} aria-label="View details"><ArrowUpRight size={17}/></button>
        </article>)}
        {displayed.length === 0 && <div className="empty-state"><div className="empty-icon"><Search size={22}/></div><h3>No opportunities here yet</h3><p>{apiMode === 'live' ? 'Try another search or run a fresh scan.' : 'Try a demo scan to see how matching cards and popups behave.'}</p><button className="secondary-button" onClick={() => scan(true)}><Sparkles size={15}/> Load demo examples</button></div>}
        {visibleEvents.length > 10 && <button className="load-more" onClick={() => setShowAll(v => !v)}>{showAll ? 'Show fewer' : `Show all ${visibleEvents.length} opportunities`} <ChevronDown size={15}/></button>}
        </div>
        </>}
        <footer className="footer"><span>HACKSCOUT <span className="footer-dot">·</span> BUILT TO FIND YOUR NEXT BUILD</span><span><ShieldCheck size={13}/> Source-first. No invented deadlines.</span></footer>
      </section>
    </main>

    {popup && <div className="modal-backdrop" onMouseDown={e => { if (e.target === e.currentTarget) setPopup(null); }}><section className="event-modal" role="dialog" aria-modal="true" aria-labelledby="modal-title"><div className="modal-top"><span className="modal-label"><span className="live-pulse"/> NEW MATCH FOUND</span><button className="icon-button" onClick={() => setPopup(null)} aria-label="Close popup"><X size={18}/></button></div><div className="modal-title-row"><div className="event-logo logo-0 modal-logo">{sourceInitial(popup.platform_label || popup.source)}</div><div><div className="event-meta">{popup.platform_label || popup.source} <span className="meta-dot">·</span> {popup.is_demo ? 'Demo example' : verificationLabel(popup.verification)}{popup.extraction_provider === 'tinyfish_agent' && <> <span className="meta-dot">·</span> TinyFish Agent extraction</>}{popup.extraction_provider === 'gemini' && <> <span className="meta-dot">·</span> Gemini extraction</>}</div><h2 id="modal-title">{popup.title}</h2></div></div><div className="modal-score-row"><div className="modal-match"><span className="score-ring big-score" style={{ '--score': `${popup.match_score || 0}%` }}><span>{popup.match_score || 0}</span></span><div><strong>Profile match</strong><span>{popup.match_reasons?.length ? popup.match_reasons.join(' · ') : 'Based on your skills and interests'}</span></div></div><span className={`verification ${popup.verification === 'page_fetched' ? 'verified-ish' : ''}`}><ShieldCheck size={14}/>{verificationLabel(popup.verification)}</span></div><p className="modal-description">{popup.snippet}</p><div className="detail-grid"><div className="detail-cell"><span><Clock3 size={13}/> REGISTRATION DEADLINE</span><strong className={!popup.deadline ? 'muted' : ''}>{dateLabel(popup.deadline)}</strong></div><div className="detail-cell"><span><Globe2 size={13}/> FORMAT</span><strong>{popup.format || 'Unknown'}</strong></div><div className="detail-cell"><span><Target size={13}/> ELIGIBILITY</span><strong>{popup.eligibility || 'Not verified'}</strong></div><div className="detail-cell"><span><MapPin size={13}/> SOURCE</span><strong className="source-domain">{popup.source}</strong></div></div>{(popup.event_date || popup.event_end_date || popup.submission_deadline) && <div className="event-timeline">{popup.event_date && <div className="event-timeline-item"><span>EVENT START</span><strong>{dateLabel(popup.event_date)}</strong></div>}{popup.event_end_date && <div className="event-timeline-item"><span>EVENT END</span><strong>{dateLabel(popup.event_end_date)}</strong></div>}{popup.submission_deadline && <div className="event-timeline-item"><span>SUBMISSION DEADLINE</span><strong>{dateLabel(popup.submission_deadline)}</strong></div>}</div>}<div className={`registration-status-note status-${popup.registration_status || 'unknown'}`}><strong>Registration status: {registrationStatusLabel(popup.registration_status)}</strong><span>{popup.status_reason || 'No current status could be confirmed from the retrieved source.'}</span></div>{popup.is_demo && <div className="demo-warning"><CircleHelp size={15}/><span>This is illustrative demo data, not a real hackathon. No application action is available.</span></div>}{!popup.is_demo && <div className="verify-note"><ShieldCheck size={15}/><span>{popup.verification === 'page_fetched' ? 'The source page was fetched. Extracted dates and eligibility still need confirmation before you apply.' : 'Only search-result information is available. Check dates and eligibility on the organizer’s page.'}</span></div>}<div className="modal-tags">{(popup.tags || []).map(t => <span className="tag" key={t}>{t}</span>)}</div><div className="modal-actions"><button className="secondary-button" onClick={() => eventAction(popup, popup.is_saved ? 'unsave' : 'save')}>{popup.is_saved ? <Check size={15}/> : <Bookmark size={15}/>} {popup.is_saved ? 'Saved' : 'Save opportunity'}</button>{!popup.is_demo ? <a className="primary-button link-button" href={popup.url} target="_blank" rel="noreferrer" onClick={() => setPopup(null)}>Open original listing <ExternalLink size={15}/></a> : <button className="primary-button" disabled title="Demo examples do not have real event pages">Original listing unavailable <ExternalLink size={15}/></button>}</div><button className="dismiss-link" onClick={() => eventAction(popup, 'dismiss')}>Not relevant? Dismiss this match</button></section></div>}

    {showProfile && <div className="modal-backdrop" onMouseDown={e => { if (e.target === e.currentTarget) setShowProfile(false); }}><section className="profile-modal" role="dialog" aria-modal="true"><div className="modal-top"><div><div className="section-kicker">PERSONALIZATION</div><h2>Scout profile</h2></div><button className="icon-button" onClick={() => setShowProfile(false)}><X size={18}/></button></div><p className="profile-help">Matching compares your skills and interests, and your resume if you add one, with each hackathon's title, tags and description.</p><div className="resume-box"><div className="resume-head"><FileText size={18}/><div><strong>Resume</strong><span>Upload a PDF and get recommendations based on your own skills.</span></div></div>{resume ? <div className="resume-loaded"><div className="resume-file"><strong>{resume.filename}</strong><span>{resume.pages} page{resume.pages === 1 ? '' : 's'} · {resume.skill_count} skills found{resume.level ? ` · looks like a ${resume.level.toLowerCase()}` : ''}</span></div><div className="resume-chips">{resume.skills.slice(0, 12).map(skill => <span className="tag" key={skill}>{skill}</span>)}</div>{resume.interests.length > 0 && <div className="resume-chips"><span className="resume-label">Interest areas</span>{resume.interests.map(area => <span className="tag accent" key={area}>{area}</span>)}</div>}<div className="resume-actions"><label className="secondary-button file-button"><Upload size={15}/> Replace PDF<input type="file" accept="application/pdf,.pdf" hidden disabled={resumeBusy} onChange={e => { uploadResume(e.target.files?.[0]); e.target.value = ''; }}/></label><button className="secondary-button" onClick={removeResume}><Trash2 size={15}/> Remove</button></div></div> : <label className="resume-drop">{resumeBusy ? <LoaderCircle size={18} className="spin"/> : <Upload size={18}/>}<span>{resumeBusy ? 'Reading your resume…' : 'Choose your resume (PDF, up to 5 MB)'}</span><input type="file" accept="application/pdf,.pdf" hidden disabled={resumeBusy} onChange={e => { uploadResume(e.target.files?.[0]); e.target.value = ''; }}/></label>}{resumeError && <p className="resume-error" role="alert">{resumeError}</p>}<p className="resume-privacy"><ShieldCheck size={13}/> Your PDF is read on this computer. Only skills and interest areas are saved, with no name, contact details or resume text. Searches use generic words like "python hackathon 2026".</p></div><label className="field-label">Skills <span>comma-separated</span><textarea value={profile.skills.join(', ')} onChange={e => setProfile(p => ({ ...p, skills: e.target.value.split(',').map(x => x.trim()).filter(Boolean) }))}/></label><label className="field-label">Interests <span>comma-separated</span><textarea value={profile.interests.join(', ')} onChange={e => setProfile(p => ({ ...p, interests: e.target.value.split(',').map(x => x.trim()).filter(Boolean) }))}/></label><div className="form-grid"><label className="field-label">Current level<select value={profile.level} onChange={e => setProfile(p => ({...p, level: e.target.value}))}><option>Student</option><option>Professional</option><option>Open to all</option></select></label><label className="field-label">Preferred region<input value={profile.location} onChange={e => setProfile(p => ({...p, location: e.target.value}))}/></label></div><label className="checkbox-row"><input type="checkbox" checked={profile.prefer_online} onChange={e => setProfile(p => ({...p, prefer_online: e.target.checked}))}/> Prefer online or remote events</label><div className="modal-actions"><button className="secondary-button" onClick={() => setShowProfile(false)}>Cancel</button><button className="primary-button" onClick={saveProfile}><Check size={15}/> Save profile</button></div></section></div>}
  </div>;
}
