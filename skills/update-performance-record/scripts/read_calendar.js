// Read-only JXA adapter for macOS Calendar. Outputs JSON to stdout.
// Usage: osascript -l JavaScript read_calendar.js --list
//        osascript -l JavaScript read_calendar.js --calendar NAME [--calendar NAME] [--from YYYY-MM-DD] [--to YYYY-MM-DD]
// Add --titles for a title inventory within the range, or --match REGEX for selected details.
// --from is inclusive; --to is exclusive, using the Mac's local calendar date.
function run(argv) {
  const selected = [];
  let from = null, to = null, list = false, titlesOnly = false, match = null;
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === '--list') { list = true; continue; }
    if (arg === '--titles') { titlesOnly = true; continue; }
    if (!['--calendar', '--from', '--to', '--match'].includes(arg) || !argv[i + 1] || argv[i + 1].startsWith('--'))
      throw new Error('Expected --list, --calendar NAME, --from YYYY-MM-DD, --to YYYY-MM-DD, --titles, or --match REGEX');
    const value = argv[++i];
    if (arg === '--calendar') selected.push(value);
    else if (arg === '--match') match = new RegExp(value, 'i');
    else {
      if (!/^\d{4}-\d{2}-\d{2}$/.test(value) || isNaN(new Date(value).getTime()) || new Date(value).toISOString().slice(0, 10) !== value)
        throw new Error('Invalid date: ' + value);
      if (arg === '--from') from = value; else to = value;
    }
  }
  if (list && argv.length !== 1) throw new Error('--list cannot be combined with export options');
  if (titlesOnly && match) throw new Error('--titles cannot be combined with --match; review the range title inventory first');
  if (from && to && from >= to) throw new Error('--from must precede --to');
  if (!list && !selected.length) throw new Error('Select calendar names from --list first');
  const app = Application('Calendar');
  const calendars = app.calendars();
  const inventory = calendars.map(c => ({name:c.name()}));
  if (list) return JSON.stringify({calendars:inventory}, null, 2);
  for (const name of selected) {
    if (inventory.filter(c => c.name === name).length !== 1)
      throw new Error('Calendar name is missing or ambiguous: ' + name);
  }
  function localDate(d) {
    function pad(n) { return String(n).padStart(2, '0'); }
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
  }
  function localMidnight(value) {
    const parts = value.split('-').map(Number);
    const date = new Date(0);
    date.setFullYear(parts[0], parts[1] - 1, parts[2]);
    date.setHours(0, 0, 0, 0);
    return date;
  }
  const dateConditions = [];
  if (from) dateConditions.push({startDate:{_greaterThanEquals:localMidnight(from)}});
  if (to) dateConditions.push({startDate:{_lessThan:localMidnight(to)}});
  function convert(p) {
    if (!(p.startDate instanceof Date) || isNaN(p.startDate.getTime()))
      throw new Error('Unreadable event date');
    return {uid:p.uid, summary:p.summary, start:p.startDate.toISOString(),
      end:p.endDate ? p.endDate.toISOString() : null,
      startLocalDate:localDate(p.startDate), endLocalDate:p.endDate ? localDate(p.endDate) : null,
      startUtcOffsetMinutes:-p.startDate.getTimezoneOffset(),
      allDay:p.alldayEvent, location:p.location, description:p.description,
      url:p.url, status:p.status, recurrence:p.recurrence, excludedDates:p.excludedDates};
  }
  const output = [];
  for (let i = 0; i < calendars.length; i++) {
    if (!selected.includes(inventory[i].name)) continue;
    const allEvents = calendars[i].events;
    const collection = dateConditions.length ? allEvents.whose({_and:dateConditions}) : allEvents;
    const initialUids = match ? collection.uid() : null;
    const summaries = titlesOnly || match ? collection.summary() : null;
    if (titlesOnly) {
      output.push({calendar:inventory[i], scannedEventObjects:summaries.length, titles:summaries});
      continue;
    }
    if (match) {
      if (initialUids.length !== summaries.length) throw new Error('Calendar changed during read; rerun export');
      const rows = [];
      for (let j = 0; j < summaries.length; j++) {
        if (!match.test(summaries[j] || '')) continue;
        const p = collection[j].properties();
        if (p.uid !== initialUids[j] || p.summary !== summaries[j])
          throw new Error('Calendar changed during read; rerun export');
        rows.push(convert(p));
      }
      if (JSON.stringify(collection.uid()) !== JSON.stringify(initialUids) ||
          JSON.stringify(collection.summary()) !== JSON.stringify(summaries))
        throw new Error('Calendar changed during read; rerun export');
      output.push({calendar:inventory[i], scannedEventObjects:summaries.length,
        events:rows.filter(r => (!from || r.startLocalDate >= from) && (!to || r.startLocalDate < to))});
      continue;
    }
    const fields = ['uid', 'summary', 'startDate', 'endDate', 'alldayEvent', 'location',
      'description', 'url', 'status', 'recurrence', 'excludedDates'];
    const columns = {};
    for (const field of fields) columns[field] = collection[field]();
    const count = columns.uid.length;
    for (const field of fields) {
      if (columns[field].length !== count) throw new Error('Calendar changed during read; rerun export');
    }
    const finalUids = collection.uid();
    if (JSON.stringify(finalUids) !== JSON.stringify(columns.uid))
      throw new Error('Calendar changed during read; rerun export');
    const rows = [];
    for (let j = 0; j < count; j++) {
      const p = {};
      for (const field of fields) p[field] = columns[field][j];
      const row = convert(p);
      if ((!from || row.startLocalDate >= from) && (!to || row.startLocalDate < to)) rows.push(row);
    }
    output.push({calendar:inventory[i], scannedEventObjects:count, events:rows});
  }
  return JSON.stringify({exportedAt:new Date().toISOString(), from, to, titlePattern:match ? match.source : null, titlesOnly,
    dateBasis:'Mac local timezone; original event timezone unavailable via Calendar scripting',
    dateFilterAppliedInCalendar:dateConditions.length > 0,
    recurrenceExpanded:false, calendars:output}, null, 2);
}
