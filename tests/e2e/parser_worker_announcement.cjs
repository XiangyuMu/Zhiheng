/* Announce, then withdraw, a live heartbeat from a worker that declares a PDF parser.
 *
 * The acceptance Worker declares PDF parsing off, so the API refuses PDF uploads.
 * Announcing another worker's declaration lets an upload be accepted while the
 * real Worker without a parser still claims it, which ADR 0005 settles as
 * `unsupported`. The heartbeat is written with the project's own recorder.
 */
const { execFileSync } = require("node:child_process");

const ANNOUNCED_WORKER_ID = "acceptance-announced-parser-worker";
const SCRIPT = `
import sys
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from zhiheng.db.session import session_scope
from zhiheng.jobs.worker_heartbeats import WorkerHeartbeat, record_worker_heartbeat

action, url, worker_id = sys.argv[1:4]
engine = create_engine(url)
try:
    with session_scope(sessionmaker(bind=engine)) as session:
        if action == "announce":
            record_worker_heartbeat(session, WorkerHeartbeat(
                worker_id=worker_id,
                role="worker",
                capabilities={"pdf_parsing": {"declared": "deepdoc", "configured": True,
                                              "detail": "announced by browser acceptance"}},
            ))
        else:
            session.execute(text("DELETE FROM worker_heartbeats WHERE worker_id = :id"),
                            {"id": worker_id})
finally:
    engine.dispose()
`;

function run(action) {
  const databaseUrl = process.env.ZHIHENG_DATABASE_URL || "";
  if (!databaseUrl.startsWith("sqlite:///")) {
    throw new Error("ZHIHENG_DATABASE_URL must name the acceptance SQLite database");
  }
  execFileSync("uv", ["run", "python", "-c", SCRIPT, action, databaseUrl, ANNOUNCED_WORKER_ID], {
    encoding: "utf8",
    stdio: ["ignore", "pipe", "inherit"],
  });
}

module.exports = {
  announceParserWorker: () => run("announce"),
  withdrawParserWorker: () => run("withdraw"),
};
