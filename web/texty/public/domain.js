export const id = () => crypto.randomUUID();
export const phoneValid = (value) => /^\+[1-9]\d{7,14}$/.test(value);
export function validateVolunteer(v) {
  if (!phoneValid(v.phone))
    throw new Error("Use an international phone number, such as +13035550123.");
  for (const field of ["first_name", "last_name"])
    if (
      typeof v[field] !== "string" ||
      !v[field].trim() ||
      v[field].length > 80
    )
      throw new Error("Enter a first and last name (80 characters or fewer).");
  return {
    first_name: v.first_name.trim(),
    last_name: v.last_name.trim(),
    phone: v.phone,
    ministry: String(v.ministry || "Welcome").slice(0, 80),
    consent: v.consent === true,
  };
}
export const intents = [
  "signup",
  "availability",
  "cancel",
  "accept",
  "question",
  "care",
  "unknown",
  "stop",
  "help",
];
export function validateDecision(d) {
  if (
    !d ||
    !intents.includes(d.intent) ||
    typeof d.summary !== "string" ||
    typeof d.reply !== "string" ||
    d.reply.length > 900 ||
    d.summary.length > 500
  )
    throw new Error("The model returned an invalid decision.");
  const confidence = Number(d.confidence);
  if (!Number.isFinite(confidence) || confidence < 0 || confidence > 1)
    throw new Error("Invalid confidence.");
  return {
    intent: d.intent,
    summary: d.summary,
    reply: d.reply,
    confidence,
    first_name: String(d.first_name || "").slice(0, 80),
    last_name: String(d.last_name || "").slice(0, 80),
    shift_id: d.shift_id || null,
    availability: String(d.availability || "").slice(0, 500),
  };
}
export function previewDecision(text) {
  const t = text.trim().toLowerCase();
  const make = (intent, summary, reply, extra = {}) => ({
    intent,
    summary,
    reply,
    confidence: 0.92,
    ...extra,
  });
  if (/^(stop|unsubscribe|cancel|end|quit)$/.test(t))
    return make("stop", "Opt out of text messages", "");
  if (t === "help")
    return make(
      "help",
      "Request help",
      "This is Text Monkey volunteer scheduling. Contact your ministry coordinator for help. Reply STOP to opt out.",
    );
  if (
    /hospital|family emergency|passed away|grief|sick|illness|lost my|suicide|hurt myself/.test(
      t,
    )
  )
    return make(
      "care",
      "Personal concern: coordinator follow-up needed",
      "Thank you for letting us know. I’ll flag your message for your coordinator.",
    );
  const name = text.match(
    /(?:join|sign up|signup|my name is|i[’']?m)\s+([\p{L}][\p{L}'-]*)\s+([\p{L}][\p{L}'-]*)/iu,
  );
  if (name)
    return make(
      "signup",
      `Create volunteer profile for ${name[1]} ${name[2]}`,
      `Thanks, ${name[1]}! Your coordinator will review your signup. Please reply YES if you agree to receive volunteer scheduling texts. Reply STOP to opt out.`,
      { first_name: name[1], last_name: name[2] },
    );
  if (/can't|cannot|can’t|won't|cancel|not make/.test(t))
    return make(
      "cancel",
      "Review cancellation and reopen the shift",
      "Thanks for letting us know. Your coordinator will confirm the shift and help find cover.",
    );
  if (/available|sundays|sunday|free|away|until|after/.test(t))
    return make(
      "availability",
      "Update volunteer availability",
      "Thank you! I’ve shared your availability with your coordinator.",
      { availability: text },
    );
  if (/^(yes|sure|i can|happy to)/.test(t))
    return make(
      "accept",
      "Review offer to serve",
      "Thank you for being willing! Your coordinator will confirm the details before assigning you.",
    );
  return make(
    "unknown",
    "Clarify the volunteer’s request",
    "Thanks for your message. Which shift or ministry are you asking about?",
    { confidence: 0.45 },
  );
}
export function seed() {
  const people = [
    ["Jen", "Hartley", "Kids", true],
    ["Marcus", "Reed", "Welcome", true],
    ["Priya", "Shah", "Kids", false],
    ["Elena", "Brooks", "Food pantry", true],
    ["Theo", "Martin", "Production", true],
    ["Sam", "Rivera", "Welcome", true],
    ["Grace", "Kim", "Kids", true],
    ["Noah", "Williams", "Production", true],
  ];
  const volunteers = people.map((p, i) => ({
    id: `v${i + 1}`,
    first_name: p[0],
    last_name: p[1],
    phone: `+1202555010${i}`,
    ministry: p[2],
    consent: true,
    status: "active",
    qualified: p[3],
    availability: i % 2 ? "Second and fourth Sundays" : "Sunday mornings",
    background_check_until: p[2] === "Kids" && p[3] ? "2027-01-31" : null,
  }));
  const shifts = [
    {
      id: "s1",
      title: "Sunday gathering",
      ministry: "Kids",
      role: "Nursery helper",
      starts_at: "2026-10-04T09:00:00-06:00",
      ends_at: "2026-10-04T10:15:00-06:00",
      required: 2,
      sensitive: true,
    },
    {
      id: "s2",
      title: "Sunday gathering",
      ministry: "Welcome",
      role: "Greeter",
      starts_at: "2026-10-04T09:00:00-06:00",
      ends_at: "2026-10-04T10:15:00-06:00",
      required: 3,
      sensitive: false,
    },
    {
      id: "s3",
      title: "Community table",
      ministry: "Food pantry",
      role: "Distribution team",
      starts_at: "2026-10-07T17:30:00-06:00",
      ends_at: "2026-10-07T19:00:00-06:00",
      required: 2,
      sensitive: false,
    },
    {
      id: "s4",
      title: "Sunday gathering",
      ministry: "Production",
      role: "Sound operator",
      starts_at: "2026-10-11T09:00:00-06:00",
      ends_at: "2026-10-11T10:15:00-06:00",
      required: 1,
      sensitive: false,
    },
  ];
  return {
    volunteers,
    shifts,
    assignments: [
      { volunteer_id: "v1", shift_id: "s1" },
      { volunteer_id: "v7", shift_id: "s1" },
      { volunteer_id: "v2", shift_id: "s2" },
      { volunteer_id: "v6", shift_id: "s2" },
      { volunteer_id: "v4", shift_id: "s3" },
      { volunteer_id: "v5", shift_id: "s4" },
    ],
    messages: [
      {
        id: "m1",
        phone: volunteers[0].phone,
        body: "I can’t make the nursery shift on Sunday.",
        direction: "inbound",
        status: "received",
        created_at: "2026-10-01T16:20:00-06:00",
      },
    ],
    proposals: [
      {
        id: "p1",
        phone: volunteers[0].phone,
        intent: "cancel",
        summary: "Jen needs cover for Sunday nursery",
        reply:
          "Thanks for letting us know, Jen. Your coordinator will help find cover.",
        shift_id: "s1",
        status: "pending",
        confidence: 0.96,
        created_at: "2026-10-01T16:20:00-06:00",
      },
    ],
  };
}
export function applyDemo(state, p) {
  if (p.status !== "pending")
    throw new Error("This action was already reviewed.");
  const v = state.volunteers.find((v) => v.phone === p.phone);
  if (p.intent === "signup") {
    if (v) throw new Error("This phone already has a profile.");
    const valid = validateVolunteer({ ...p, consent: false });
    state.volunteers.unshift({
      ...valid,
      id: id(),
      status: "pending",
      qualified: false,
      availability: "Not provided",
    });
  }
  if (p.intent === "availability") {
    if (!v) throw new Error("Create this volunteer’s profile first.");
    v.availability = p.availability;
  }
  if (p.intent === "cancel") {
    if (
      !p.shift_id ||
      !state.assignments.some(
        (a) => a.shift_id === p.shift_id && a.volunteer_id === v?.id,
      )
    )
      throw new Error("Choose an assigned shift before approving.");
    state.assignments = state.assignments.filter(
      (a) => !(a.shift_id === p.shift_id && a.volunteer_id === v.id),
    );
  }
  p.status = "approved";
  if (
    p.reply &&
    v?.status !== "opted_out" &&
    !(state.optouts || []).includes(p.phone)
  )
    state.messages.push({
      id: id(),
      phone: p.phone,
      body: p.reply,
      direction: "outbound",
      status: "draft",
      created_at: new Date().toISOString(),
    });
}

// Offline preview of the text signup conversation. Live mode uses Gloo.
export function processDemoSignup(state, phone, text) {
  const word = text.trim().toUpperCase();
  if ((state.optouts || []).includes(phone)) return false;
  const existing = state.volunteers.find((v) => v.phone === phone);
  const reply = (body) =>
    state.messages.push({
      id: id(),
      phone,
      body: body.replace(/[🐵🐒]\ufe0f?/gu, "").trim() + " 🐒",
      direction: "outbound",
      status: "simulated",
      created_at: new Date().toISOString(),
    });
  if (existing?.signup_pending) {
    if (["YES", "Y"].includes(word)) {
      existing.consent = true;
      existing.status = "active";
      existing.signup_pending = false;
      existing.onboarding_stage = "interests";
      reply("What would you like to help with? Reply with a role or ministry, or ANY.");
    } else if (["NO", "N"].includes(word)) {
      existing.signup_pending = false;
      existing.status = "paused";
    } else
      reply(
        "Reply YES to receive volunteer scheduling texts and finish signup.",
      );
    return true;
  }
  if (existing?.onboarding_stage && !["STOP", "HELP", "START"].includes(word) && !/hospital|emergency|passed away|suicide|hurt myself/i.test(text)) {
    if (existing.onboarding_stage === "interests") {
      existing.ministry = word === "ANY" ? "Flexible" : text.trim();
      existing.onboarding_stage = "availability";
      reply("When can you serve, and how often? For example: Sundays at 9am, twice a month. Or FLEXIBLE.");
      return true;
    }
    if (existing.onboarding_stage === "availability") {
      existing.availability = text.trim();
      existing.onboarding_stage = "complete";
      reply(`You’re all set, ${existing.first_name}! We’ve saved your preferences. When a shift matches, we’ll text you the details and ask if you can take it.`);
      return true;
    }
  }
  if (
    existing ||
    /hospital|emergency|passed away|suicide|hurt myself/i.test(text)
  )
    return false;
  state.signup_sessions ||= {};
  if (["JOIN", "SIGNUP", "SIGN UP"].includes(word)) {
    state.signup_sessions[phone] = true;
    reply("Welcome to Text Monkey! What is your first and last name? Reply STOP to stop or HELP for help.");
    return true;
  }
  const decision = previewDecision(
    state.signup_sessions[phone] ? `JOIN ${text}` : text,
  );
  if (decision.intent !== "signup") return false;
  state.volunteers.unshift({
    id: id(),
    phone,
    first_name: decision.first_name,
    last_name: decision.last_name,
    ministry: "Not set",
    consent: false,
    status: "pending",
    qualified: false,
    signup_pending: true,
    availability: "Not provided",
  });
  delete state.signup_sessions[phone];
  const introduced = state.messages.some(m => m.phone === phone && m.direction === "outbound");
  reply(`Thanks, ${decision.first_name}! Reply YES to receive volunteer scheduling texts.` +
    (introduced ? "" : " Reply STOP to stop or HELP for help."));
  return true;
}

// Explicit offline coordinator actions; never a prediction of live eligibility.
export function demoBooking(state, shiftId, volunteerId, action) {
  const shift = state.shifts.find(s => s.id === shiftId);
  const volunteer = state.volunteers.find(v => v.id === volunteerId);
  if (!shift || !volunteer) throw new Error('Choose a sample shift and volunteer.');
  const assigned = state.assignments.some(a => a.shift_id === shiftId && a.volunteer_id === volunteerId);
  if (action === 'cancel') {
    if (!assigned) throw new Error('This volunteer is not booked for the selected shift.');
    state.assignments = state.assignments.filter(a => !(a.shift_id === shiftId && a.volunteer_id === volunteerId));
    return 'Sample booking cancelled. The selected slot is open; no replacement has been assigned.';
  }
  if (action !== 'book') throw new Error('Choose a supported sample action.');
  if (assigned) throw new Error('This volunteer is already booked for this shift.');
  if (state.assignments.filter(a => a.shift_id === shiftId).length >= shift.required) throw new Error('This shift is already fully staffed.');
  if (!volunteer.consent || volunteer.status !== 'active' || (state.optouts || []).includes(volunteer.phone)) throw new Error('The sample volunteer must be active and opted in.');
  if (!volunteer.qualified || volunteer.ministry !== shift.ministry) throw new Error('Choose a qualified sample volunteer in this ministry.');
  if (shift.sensitive && (!volunteer.background_check_until || volunteer.background_check_until < shift.starts_at.slice(0,10))) throw new Error('This sample role requires current clearance.');
  if (state.assignments.some(a => {
    if (a.volunteer_id !== volunteerId) return false;
    const other = state.shifts.find(s => s.id === a.shift_id);
    return other && new Date(other.starts_at) < new Date(shift.ends_at) && new Date(shift.starts_at) < new Date(other.ends_at);
  })) throw new Error('This sample volunteer has an overlapping booking.');
  state.assignments.push({shift_id:shiftId, volunteer_id:volunteerId});
  return 'Sample booking saved. Coverage updated locally; no real text sent.';
}
