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

fn run(cli: &Cli) -> Result<()> {
    match &cli.command {
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
        Tool::Tools { json: structured } => {
            if *structured {
                println!(
                    "{}",
                    json!({
                        "schema_version":1,
                        "tools":[{
                            "name":"search",
                            "command":"grepglint search --json <query>",
                        "use_when":"You know the concept but not the exact identifier or location in the current Git checkout. Regular clones and linked worktrees both work.",
                            "inputs":{"query":"Words or identifiers; no regex or FTS operators.","limit":"Optional --limit, 1 to 20, default 5."},
                            "returns":"Ranked file paths, line ranges, symbols, short excerpts, content identities, and freshness counts.",
                            "follow_up":"Use rg for exact strings or identifiers, then read the relevant code.",
                            "side_effects":"Starts a local daemon if needed and updates a bounded, disposable machine-local cache. Does not edit the repository or use the network."
                        }]
                    })
                );
            } else {
                println!(
                    "search  Find likely code regions when you know the concept but not its identifier or location.\n        grepglint search --json \"refresh token validation\"\n\nUse rg for exact matches, then read the relevant code.\nRun grepglint tools --json for the machine-readable tool catalog."
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
            Tool::Search { json: true, .. } | Tool::Status { json: true }
        ) {
            println!("{}", json!({"error":format!("{error:#}")}));
        } else {
            eprintln!("grepglint: {error:#}");
        }
        std::process::exit(1);
    }
}
