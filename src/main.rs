use anyhow::Result;
use clap::{Parser, Subcommand};
use grepglint::{
    daemon::{self, Config},
    protocol::{Request, VERSION},
};
use serde_json::json;

#[derive(Parser)]
#[command(
    version,
    about = "Local code discovery for coding agents",
    after_help = "Start with `grepglint tools --json` to discover available tools.\nUse rg for exact strings and identifiers."
)]
struct Cli {
    #[command(subcommand)]
    command: Tool,
}

#[derive(Subcommand)]
enum Tool {
    /// Install or verify a managed release; not an agent exploration tool
    Setup(grepglint::setup::Options),
    /// Rank likely code regions for a concept or group of identifiers
    Search {
        query: String,
        /// Emit a structured result with paths, excerpts, identities, and indexing counts
        #[arg(long)]
        json: bool,
        /// Number of regions to return, from 1 to 20
        #[arg(short = 'n', long, default_value_t = 5)]
        limit: usize,
        /// Print indexing counts and elapsed time to stderr
        #[arg(long)]
        stats: bool,
    },
    /// Retain UTF-8 stdin and retrieve it outside or inside Git repositories
    Output {
        #[command(subcommand)]
        command: OutputTool,
    },
    /// List available tools and when an agent should use each one
    Tools {
        #[arg(long)]
        json: bool,
    },
    /// Identify the running daemon without starting it
    Status {
        #[arg(long)]
        json: bool,
    },
    /// Stop the identified daemon safely; searches can restart it afterward
    Shutdown {
        /// Refuse to stop a different instance than the one reported by status
        #[arg(long)]
        instance: Option<String>,
    },
    #[command(name = "__daemon", hide = true)]
    Daemon,
}

#[derive(Subcommand)]
enum OutputTool {
    /// Pass small stdin through unchanged; retain larger text with a bounded preview
    Bounce,
    /// Execute a finite shell command once with bounded output retention and exact recovery
    Exec(grepglint::output::execution::Options),
    /// Read retained text from the beginning or an opaque continuation cursor
    Page {
        handle: String,
        #[arg(long)]
        cursor: Option<String>,
        /// Emit exact content in JSON, including original line endings and controls
        #[arg(long)]
        json: bool,
    },
    /// Find likely sections of retained output; exact paging remains available
    Search {
        handle: String,
        query: String,
        #[arg(short = 'n', long, default_value_t = 5)]
        limit: usize,
        #[arg(long)]
        json: bool,
    },
    /// Erase owned retained output; preserve repository caches and unrelated files
    Purge,
}

fn run(cli: &Cli) -> Result<()> {
    match &cli.command {
        Tool::Setup(options) => grepglint::setup::run(options)?,
        Tool::Search {
            query,
            json,
            limit,
            stats,
        } => {
            let request = Request {
                version: VERSION,
                cwd: std::env::current_dir()?.to_string_lossy().into_owned(),
                query: query.clone(),
                limit: *limit,
            };
            let response = daemon::search(&Config::from_env()?, &request)?;
            if *json {
                println!("{}", serde_json::to_string(&response)?);
            } else {
                if response.results.is_empty() {
                    println!("No matching regions. Try fewer terms or confirm exact text with rg.");
                }
                for (index, result) in response.results.iter().enumerate() {
                    println!(
                        "{}. {}:{}-{}",
                        index + 1,
                        printable(&result.path),
                        result.start_line,
                        result.end_line
                    );
                    if let Some(symbol) = &result.symbol {
                        println!("   {}", printable(symbol));
                    }
                    println!("   score: {:.6}", result.score);
                    for (offset, line) in result.content.lines().enumerate() {
                        println!(
                            "   {:>4} | {}",
                            result.snippet_start_line + offset,
                            printable(line)
                        );
                    }
                    if result.truncated {
                        println!("   ... excerpt; read the region for full context");
                    }
                    println!();
                }
            }
            if *stats {
                eprintln!("{}", serde_json::to_string(&response.stats)?);
            }
        }
        Tool::Output { command } => {
            if let OutputTool::Exec(options) = command {
                let code = grepglint::output::execution::run(options)?;
                std::process::exit(code);
            }
            let config = Config::from_env()?;
            match command {
                OutputTool::Exec(_) => unreachable!(),
                OutputTool::Bounce => grepglint::output::bounce(&config)?,
                OutputTool::Page {
                    handle,
                    cursor,
                    json,
                } => {
                    let page =
                        grepglint::output::Store::open(&config)?.page(handle, cursor.as_deref())?;
                    let rendered = if *json {
                        format!("{}\n", serde_json::to_string(&page)?)
                    } else {
                        let continuation = if let Some(cursor) = &page.next_cursor {
                            format!(
                                "Continue: grepglint output page {} --cursor {}",
                                handle, cursor
                            )
                        } else {
                            "End of output.".to_owned()
                        };
                        format!(
                            "{}\n{}\n",
                            grepglint::output::printable(&page.content),
                            continuation
                        )
                    };
                    grepglint::output::write_stdout(rendered.as_bytes())?;
                }
                OutputTool::Search {
                    handle,
                    query,
                    limit,
                    json,
                } => {
                    let response = grepglint::output::search(&config, handle, query, *limit)?;
                    let rendered = if *json {
                        format!("{}\n", serde_json::to_string(&response)?)
                    } else {
                        let mut text = format!(
                            "Output {}: {} bytes, {} lines\n",
                            response.handle, response.bytes, response.lines
                        );
                        if response.results.is_empty() {
                            text.push_str(
                                "No matching sections. Try fewer terms or page the original.\n",
                            );
                        }
                        for (index, result) in response.results.iter().enumerate() {
                            let r = &result.region;
                            text.push_str(&format!("{}. Lines {}-{}, bytes {}..{}, score {:.6}\n{}\n{}{}\nInspect: {}\n\n",
                                index + 1, r.excerpt_start_line, r.excerpt_end_line, r.excerpt_start_byte, r.excerpt_end_byte, r.score,
                                grepglint::output::printable(&r.content),
                                if r.clipped_before { "[earlier chunk text omitted] " } else { "" },
                                if r.clipped_after { "[later chunk text omitted]" } else { "" }, result.page_command));
                        }
                        text.push_str(&format!("Lexical OR matches are suggestions, not exact matches or guaranteed answers.\nRead all original bytes: {}\n", response.page_command));
                        text
                    };
                    grepglint::output::write_stdout(rendered.as_bytes())?;
                }
                OutputTool::Purge => {
                    grepglint::output::Store::open(&config)?.purge()?;
                    println!("Owned output contents purged. Repository caches are unchanged.");
                }
            }
        }
        Tool::Tools { json: structured } => {
            if *structured {
                println!(
                    "{}",
                    json!({
                        "schema_version":1,
                        "tools":[{
                            "name":"search",
                            "command":"grepglint search --json <query>",
                        "use_when":"Find likely implementation regions in the current Git checkout when you have related words or identifiers but no focused file, or when broad text search returns too many matches. Use a compact group of relevant terms, for example migration dependency graph or request middleware exception. Regular clones and linked worktrees both work.",
                            "inputs":{"query":"Related words or identifiers; no regex or FTS operators. For example: migration dependency graph.","limit":"Optional --limit, 1 to 20, default 5."},
                            "returns":"Ranked file paths, line ranges, symbols, short excerpts, content identities, and freshness counts. Results are lexical suggestions, not exhaustive references or guaranteed answers.",
                            "follow_up":"Read the relevant regions to verify them. Use rg for exact strings, regex, or all occurrences; directly read a file when its location is already known. If indexing fails, use rg and file reads; repeating the same query will not fix a capacity failure.",
                            "side_effects":"The first search builds a bounded local index and may take several seconds. Starts a local daemon if needed and updates a bounded, disposable machine-local cache. Does not edit the repository or use the network."
                        }, {
                            "name":"output exec", "command":"grepglint output exec --shell /bin/sh --command <command> [--profile preview16k|preview32k|unchanged]",
                            "use_when":"Run a finite noninteractive command once and retrieve omitted output through search or exact pages.",
                            "inputs":"Explicit shell and command; optional --cwd, repeatable --env NAME=VALUE, --timeout-seconds. Inherits caller permissions, environment and limits. No TTY or interactive stdin.",
                            "returns":"Merged stdout/stderr unchanged or an incomplete bounded preview with producer status and recovery commands. Original exit code, or 128+signal. One JSON metadata record on stderr.",
                            "side_effects":"Runs the supplied command once. Retains accepted UTF-8 output up to 8 MiB in private bounded local storage, subject to expiry and eviction. Capture failure forwards original output; caller truncation still applies."
                        }, {
                            "name":"output bounce", "command":"producer | grepglint output bounce",
                            "use_when":"Keep large UTF-8 stdout out of the initial response and retrieve all of it later.",
                            "inputs":"Finite UTF-8 stdin without NUL, at most 8 MiB. Use 2>&1 to merge stderr and pipefail to preserve producer failure.",
                            "returns":"Original bytes through 4096 bytes; otherwise a bounded preview and immutable handle.",
                            "side_effects":"Retains private local output for at most one hour, subject to eviction. Consumed input may need rerunning on failure."
                        }, {
                            "name":"output page", "command":"grepglint output page <handle> --json [--cursor <cursor>]",
                            "use_when":"Retrieve every byte from a retained output, beginning at its start.",
                            "returns":"Exact decoded content with a next cursor or end-of-output.",
                            "side_effects":"Cleans expired output on demand."
                        }, {
                            "name":"output search", "command":"grepglint output search <handle> <query> --json [--limit 1..20]",
                            "use_when":"Find likely sections in one retained output without reading every page. Lexical OR ranking does not guarantee exact matches or an answer.",
                            "inputs":"Query up to 2000 bytes and 32 expanded OR terms; default 5 results, maximum 20.",
                            "returns":"Ranked excerpts with original byte/line ranges, handle identity, clipping indicators and exact paging commands. Empty results include paging guidance.",
                            "side_effects":"Cleans expired output; starts the local daemon if needed and uses a disposable in-memory index within its shared 30-second/64 MiB SQLite budget. No repository registration or persistent output index."
                        }, {
                            "name":"output purge", "command":"grepglint output purge",
                            "use_when":"Erase owned retained output while preserving repository caches.",
                            "side_effects":"Deletes output contents; fails while capture remains active."
                        }]
                    })
                );
            } else {
                println!(
                    "search  Find likely implementation regions from related words or identifiers when no file is known or broad text search returns too many matches.\n        grepglint search --json \"refresh token validation\"\n\nResults are lexical suggestions. Read the relevant code to verify them. Use rg for exact strings, regex, or all occurrences; read known files directly.\noutput exec    Execute a finite command once with recoverable output.\noutput bounce  Retain large stdin with a bounded preview.\noutput search  Find relevant sections; use paging to inspect original bytes.\noutput page    Retrieve exact retained text with --json.\noutput purge   Erase owned output contents.\nRun grepglint tools --json for the machine-readable tool catalog."
                );
            }
        }
        Tool::Status { json } => {
            let health = grepglint::maintenance::status(&Config::from_env()?)?;
            if *json {
                println!("{}", serde_json::to_string(&health)?);
            } else if let Some(health) = health {
                println!(
                    "Running Grepglint {}\nBuild SHA-256: {}\nProtocol: {}\nInstance: {}\nCache: {}\nDatabase limit: {} bytes\nIdle timeout: {} seconds",
                    health.build_version,
                    health.executable_sha256,
                    health.protocol_version,
                    health.instance,
                    printable(&health.cache_directory),
                    health.database_bytes,
                    health.idle_seconds
                );
            } else {
                println!("No daemon is running.");
            }
        }
        Tool::Shutdown { instance } => {
            if grepglint::maintenance::shutdown(&Config::from_env()?, instance.as_deref())? {
                println!("Daemon stopped. The next search can start it again.");
            } else {
                println!("No daemon is running.");
            }
        }
        Tool::Daemon => {
            let config = Config::from_env()?;
            if let Err(error) = daemon::serve(&config) {
                daemon::record_startup_error(&config, &format!("{error:#}"));
                return Err(error);
            }
        }
    }
    Ok(())
}

fn printable(text: &str) -> String {
    text.chars()
        .flat_map(|c| {
            if c.is_control() {
                c.escape_default().collect::<Vec<_>>()
            } else {
                vec![c]
            }
        })
        .collect()
}

fn main() {
    let cli = Cli::parse();
    if let Err(error) = run(&cli) {
        if matches!(
            cli.command,
            Tool::Search { json: true, .. }
                | Tool::Status { json: true }
                | Tool::Output {
                    command: OutputTool::Page { json: true, .. }
                        | OutputTool::Search { json: true, .. }
                }
        ) {
            println!("{}", json!({"error":format!("{error:#}")}));
        } else {
            eprintln!("grepglint: {error:#}");
        }
        std::process::exit(1);
    }
}
