-- Fresh Texty project only. The private schema is not exposed through the Data API.

create schema if not exists texty;

revoke all on schema texty from public,anon,authenticated;


CREATE TABLE texty.agent_runs (
	id SERIAL NOT NULL,
	agent VARCHAR(40) NOT NULL,
	trigger VARCHAR(200) NOT NULL,
	started_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	ended_at TIMESTAMP WITHOUT TIME ZONE,
	model VARCHAR(80),
	input_tokens INTEGER NOT NULL,
	output_tokens INTEGER NOT NULL,
	steps INTEGER NOT NULL,
	outcome VARCHAR(200),
	PRIMARY KEY (id)
)

;

alter table texty.agent_runs enable row level security;

revoke all on texty.agent_runs from public,anon,authenticated;


CREATE TABLE texty.approvals (
	id SERIAL NOT NULL,
	kind VARCHAR(40) NOT NULL,
	payload JSON NOT NULL,
	status VARCHAR(20) NOT NULL,
	requested_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	decided_at TIMESTAMP WITHOUT TIME ZONE,
	decided_by VARCHAR(120),
	via VARCHAR(10),
	PRIMARY KEY (id)
)

;

alter table texty.approvals enable row level security;

revoke all on texty.approvals from public,anon,authenticated;


CREATE TABLE texty.event_types (
	id SERIAL NOT NULL,
	name VARCHAR(80) NOT NULL,
	title_patterns JSON NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (name)
)

;

alter table texty.event_types enable row level security;

revoke all on texty.event_types from public,anon,authenticated;


CREATE TABLE texty.flags (
	id SERIAL NOT NULL,
	kind VARCHAR(20) NOT NULL,
	type VARCHAR(40) NOT NULL,
	summary TEXT NOT NULL,
	evidence JSON NOT NULL,
	suggested_action TEXT,
	status VARCHAR(20) NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	PRIMARY KEY (id)
)

;

alter table texty.flags enable row level security;

revoke all on texty.flags from public,anon,authenticated;


CREATE TABLE texty.policies (
	key VARCHAR(80) NOT NULL,
	value JSON NOT NULL,
	PRIMARY KEY (key)
)

;

alter table texty.policies enable row level security;

revoke all on texty.policies from public,anon,authenticated;


CREATE TABLE texty.roles (
	id SERIAL NOT NULL,
	name VARCHAR(80) NOT NULL,
	ministry VARCHAR(80) NOT NULL,
	required_qualifications JSON NOT NULL,
	criticality VARCHAR(20) NOT NULL,
	fill_policy VARCHAR(20) NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (name)
)

;

alter table texty.roles enable row level security;

revoke all on texty.roles from public,anon,authenticated;


CREATE TABLE texty.volunteers (
	id SERIAL NOT NULL,
	name VARCHAR(120) NOT NULL,
	phone VARCHAR(20) NOT NULL,
	sms_opt_in BOOLEAN NOT NULL,
	status VARCHAR(20) NOT NULL,
	is_coordinator BOOLEAN NOT NULL,
	is_pastor BOOLEAN NOT NULL,
	preferences JSON NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	PRIMARY KEY (id)
)

;

CREATE UNIQUE INDEX ix_texty_volunteers_phone ON texty.volunteers (phone);

alter table texty.volunteers enable row level security;

revoke all on texty.volunteers from public,anon,authenticated;


CREATE TABLE texty.agent_steps (
	id SERIAL NOT NULL,
	run_id INTEGER NOT NULL,
	step_no INTEGER NOT NULL,
	type VARCHAR(20) NOT NULL,
	tool_name VARCHAR(60),
	arguments JSON,
	result JSON,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(run_id) REFERENCES texty.agent_runs (id)
)

;

CREATE INDEX ix_texty_agent_steps_run_id ON texty.agent_steps (run_id);

alter table texty.agent_steps enable row level security;

revoke all on texty.agent_steps from public,anon,authenticated;


CREATE TABLE texty.availability (
	id SERIAL NOT NULL,
	volunteer_id INTEGER NOT NULL,
	month VARCHAR(7) NOT NULL,
	available_dates JSON NOT NULL,
	unavailable_dates JSON NOT NULL,
	raw_reply TEXT,
	parsed_at TIMESTAMP WITHOUT TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(volunteer_id) REFERENCES texty.volunteers (id)
)

;

CREATE INDEX ix_texty_availability_volunteer_id ON texty.availability (volunteer_id);

alter table texty.availability enable row level security;

revoke all on texty.availability from public,anon,authenticated;


CREATE TABLE texty.escalations (
	id SERIAL NOT NULL,
	category VARCHAR(30) NOT NULL,
	severity VARCHAR(10) NOT NULL,
	summary TEXT NOT NULL,
	related_ids JSON NOT NULL,
	assigned_to INTEGER,
	status VARCHAR(20) NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(assigned_to) REFERENCES texty.volunteers (id)
)

;

alter table texty.escalations enable row level security;

revoke all on texty.escalations from public,anon,authenticated;


CREATE TABLE texty.events (
	id SERIAL NOT NULL,
	gcal_event_id VARCHAR(120),
	title VARCHAR(200) NOT NULL,
	event_type_id INTEGER,
	starts_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	ends_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	status VARCHAR(20) NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(event_type_id) REFERENCES texty.event_types (id)
)

;

CREATE INDEX ix_texty_events_starts_at ON texty.events (starts_at);

alter table texty.events enable row level security;

revoke all on texty.events from public,anon,authenticated;


CREATE TABLE texty.messages (
	id SERIAL NOT NULL,
	direction VARCHAR(5) NOT NULL,
	volunteer_id INTEGER,
	phone VARCHAR(20) NOT NULL,
	body TEXT NOT NULL,
	kind VARCHAR(20) NOT NULL,
	purpose VARCHAR(40),
	provider_sid VARCHAR(64),
	status VARCHAR(20) NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(volunteer_id) REFERENCES texty.volunteers (id)
)

;

CREATE INDEX ix_texty_messages_phone ON texty.messages (phone);

CREATE INDEX ix_texty_messages_volunteer_id ON texty.messages (volunteer_id);

alter table texty.messages enable row level security;

revoke all on texty.messages from public,anon,authenticated;


CREATE TABLE texty.qualifications (
	id SERIAL NOT NULL,
	volunteer_id INTEGER NOT NULL,
	type VARCHAR(50) NOT NULL,
	status VARCHAR(20) NOT NULL,
	verified_by VARCHAR(120),
	verified_at TIMESTAMP WITHOUT TIME ZONE,
	expires_on DATE,
	PRIMARY KEY (id),
	FOREIGN KEY(volunteer_id) REFERENCES texty.volunteers (id)
)

;

CREATE INDEX ix_texty_qualifications_volunteer_id ON texty.qualifications (volunteer_id);

alter table texty.qualifications enable row level security;

revoke all on texty.qualifications from public,anon,authenticated;


CREATE TABLE texty.role_recipes (
	id SERIAL NOT NULL,
	event_type_id INTEGER NOT NULL,
	role_id INTEGER NOT NULL,
	count INTEGER NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(event_type_id) REFERENCES texty.event_types (id),
	FOREIGN KEY(role_id) REFERENCES texty.roles (id)
)

;

CREATE INDEX ix_texty_role_recipes_event_type_id ON texty.role_recipes (event_type_id);

alter table texty.role_recipes enable row level security;

revoke all on texty.role_recipes from public,anon,authenticated;


CREATE TABLE texty.shifts (
	id SERIAL NOT NULL,
	event_id INTEGER NOT NULL,
	role_id INTEGER NOT NULL,
	slot_index INTEGER NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(event_id) REFERENCES texty.events (id),
	FOREIGN KEY(role_id) REFERENCES texty.roles (id)
)

;

CREATE INDEX ix_texty_shifts_role_id ON texty.shifts (role_id);

CREATE INDEX ix_texty_shifts_event_id ON texty.shifts (event_id);

alter table texty.shifts enable row level security;

revoke all on texty.shifts from public,anon,authenticated;


CREATE TABLE texty.assignments (
	id SERIAL NOT NULL,
	shift_id INTEGER NOT NULL,
	volunteer_id INTEGER NOT NULL,
	status VARCHAR(20) NOT NULL,
	source VARCHAR(20) NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(volunteer_id) REFERENCES texty.volunteers (id),
	FOREIGN KEY(shift_id) REFERENCES texty.shifts (id)
)

;

CREATE INDEX ix_texty_assignments_volunteer_id ON texty.assignments (volunteer_id);

CREATE INDEX ix_texty_assignments_shift_id ON texty.assignments (shift_id);

alter table texty.assignments enable row level security;

revoke all on texty.assignments from public,anon,authenticated;


CREATE TABLE texty.fill_requests (
	id SERIAL NOT NULL,
	shift_id INTEGER NOT NULL,
	cancelled_assignment_id INTEGER,
	urgency VARCHAR(20) NOT NULL,
	state VARCHAR(30) NOT NULL,
	current_tranche INTEGER NOT NULL,
	next_action_at TIMESTAMP WITHOUT TIME ZONE,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	closed_at TIMESTAMP WITHOUT TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(cancelled_assignment_id) REFERENCES texty.assignments (id),
	FOREIGN KEY(shift_id) REFERENCES texty.shifts (id)
)

;

CREATE INDEX ix_texty_fill_requests_shift_id ON texty.fill_requests (shift_id);

alter table texty.fill_requests enable row level security;

revoke all on texty.fill_requests from public,anon,authenticated;


CREATE TABLE texty.outreach (
	id SERIAL NOT NULL,
	fill_request_id INTEGER NOT NULL,
	volunteer_id INTEGER NOT NULL,
	tranche INTEGER NOT NULL,
	message_id INTEGER,
	response VARCHAR(20) NOT NULL,
	responded_at TIMESTAMP WITHOUT TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(fill_request_id) REFERENCES texty.fill_requests (id),
	FOREIGN KEY(message_id) REFERENCES texty.messages (id),
	FOREIGN KEY(volunteer_id) REFERENCES texty.volunteers (id)
)

;

CREATE INDEX ix_texty_outreach_fill_request_id ON texty.outreach (fill_request_id);

CREATE INDEX ix_texty_outreach_volunteer_id ON texty.outreach (volunteer_id);

alter table texty.outreach enable row level security;

revoke all on texty.outreach from public,anon,authenticated;
