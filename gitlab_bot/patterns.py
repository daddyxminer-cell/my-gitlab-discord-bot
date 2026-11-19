"""
Everything the bot knows about "what a new PoW coin / node project looks like".

Three kinds of evidence are used (this is what makes the bot look INSIDE a project,
not only at its name/description):

  TEXT   - README + docs + description + topics  (words people write)
  TREE   - file and folder names                  (chainparams.cpp, pow.cpp, randomx/ ...)
  CODE   - contents of a few key source/config files (nPowTargetSpacing, pchMessageStart ...)

Edit the lists below to tune the bot. Use  python bot.py --explain GROUP/PROJECT  to see
which of these fired for a real project.
"""
from __future__ import annotations

import re

# --------------------------------------------------------------------------- #
# Algorithms
# --------------------------------------------------------------------------- #
# Names that (almost) only mean "mining algorithm".
ALGO_STRONG = [
    "RandomX", "RandomARQ", "RandomSFX", "KawPow", "ProgPow", "Ethash", "Etchash", "SHA256d",
    "SHA-256d", "Scrypt", "Equihash", "X16R", "X16RV2", "X25X", "kHeavyHash", "HeavyHash",
    "Autolykos", "Yespower", "YesCrypt", "GhostRider", "Cuckatoo", "Cuckaroo", "Cuckoo Cycle",
    "FiroPow", "MeowPow", "KarlsenHash", "FishHash", "XelisHash", "Verthash", "NeoScrypt",
    "Lyra2REv3", "CryptoNight", "Hashimoto", "Dagger-Hashimoto", "Blake3-PoW", "SHA3x",
    "Equihash125_4", "Equihash192_7", "Equihash144_5", "VerusHash", "Minotaurx",
]
# Names that are common outside mining too (only count when other PoW words are present).
ALGO_WEAK = [
    "SHA256", "SHA-256", "SHA3", "X11", "X13", "X15", "Blake3", "Blake2b", "Keccak", "Groestl",
    "Skein", "Qubit", "Lyra2", "Argon2d", "Argon2id", "Cuckoo",
]


def _algo_re(names):
    return re.compile(r"(?<![\w-])(" + "|".join(re.escape(a) for a in names) + r")(?![\w])", re.I)


ALGO_STRONG_RE = _algo_re(ALGO_STRONG)
ALGO_WEAK_RE = _algo_re(ALGO_WEAK)

# --------------------------------------------------------------------------- #
# TEXT patterns  (applied to README / docs / description / topics)
# --------------------------------------------------------------------------- #
# "pow" must not be Math.pow / pow(x, y)
_POW_WORD = r"proof[\s-]*of[\s-]*work|(?<![.\w])pow(?![\w(])"

POW_PATTERNS = {
    "proof-of-work": _POW_WORD,
    "algorithm": ALGO_STRONG_RE.pattern,
    "mining": r"\b(cpu|gpu|asic|fpga)[\s-]*(mining|miners?|mineable|minable|friendly|resistant)\b"
              r"|\bmine (it|blocks|coins|the (chain|network))\b|\bmineable\b|\bminable\b|\bminers? (reward|software|binary|release)",
    "miner": r"\bxmrig\b|\bcpuminer\b|\bsrbminer\b|\blolminer\b|\bt-?rex\b|\bnbminer\b|\bgminer\b|\bbzminer\b|\bteamredminer\b",
    "pool": r"\bmining pool|\bstratum\b|\bpplns\b|\bsolo[\s-]*min(e|ing)\b|\bpool mining\b",
    "hashrate": r"\bhash[\s-]*rate\b|\bnethash\b|\bnetwork hash\b|\bdifficulty (adjustment|retarget(ing)?|algorithm)\b"
                r"|\blwma\b|\bdaa\b|\bgetblocktemplate\b",
}
# These make a PoW claim believable. The bare word "PoW" alone is not enough.
POW_STRONG_KEYS = {"algorithm", "mining", "miner", "pool", "hashrate"}

# Signals that the project is an actual coin / chain (not a library or a demo).
CHAIN_STRONG = {
    "genesis": r"\bgenesis (block|hash|coinbase|file|config|timestamp)\b",
    "reward": r"\bblock reward\b|\bblock subsidy\b|\breward per block\b|\bcoinbase reward\b|\binitial reward\b",
    "block time": r"\bblock[\s-]*time\b|\btarget (block )?spacing\b|\bseconds per block\b|\bblock interval\b",
    "supply": r"\b(max(imum)?|total|circulating|initial) supply\b|\bemission\b|\bhalving\b|\bsupply cap\b",
    "premine": r"\b(no|zero|without)[\s-]*pre[\s-]*(mine|sale|allocation)\b|\bfair[\s-]*launch\b|\bpremine\b|\bdev (fee|fund)\b",
    "network": r"\bmainnet\b|\btestnet\b|\bdevnet\b|\bregtest\b|\bseed ?nodes?\b|\bbootstrap nodes?\b|\bboot ?nodes?\b"
               r"|\bp2p port\b|\brpc port\b|\bchain[\s-]?id\b",
    "address": r"\baddress prefix\b|\bticker\b|\bcoin name\b|\bbase58\b|\bbech32\b",
    "node": r"\b(full|seed|reference) node\b|\bnode software\b|\b\w+d daemon\b|\bdaemon\b",
    "consensus": r"\bconsensus (rules?|params?|parameters?|mechanism|layer|engine)\b|\bhard fork\b|\bmempool\b|\bcoinbase\b|\butxo\b",
    "coin": r"\bcryptocurrency\b|\baltcoin\b|\bblockchain\b|\blayer[\s-]*1\b|\bl1\b|\bcrypto[\s-]?coin\b|\bnative (coin|token)\b",
}
# Weak chain signals: at most 2 points in total, so a README full of "wallet" doesn't win.
CHAIN_WEAK = {
    "wallet": r"\bwallet\b",
    "explorer": r"\bblock explorer\b|\bexplorer\b",
    "coin-word": r"\bcoin\b",
    "peers": r"\bpeers?\b",
}
# Looked at in name / description / topics only ("xyzcoin", "foochain").
HEAD_CHAIN = r"\w(coin|chain|cash|net)\b|\bcoin\b|\bchain\b"

# Projects where running a node is the point (masternodes / validators / DePIN / testnet nodes).
NODE_PATTERNS = {
    "masternode": r"\bmaster[\s-]*nodes?\b|\bsuper[\s-]*nodes?\b|\bservice[\s-]*nodes?\b|\bfunding[\s-]*nodes?\b",
    "node rewards": r"\bnode rewards?\b|\brewards? for (running|hosting|operating) (a |your )?nodes?\b|\bnode operators?\b|\bearn(ing)? (by|from) running\b",
    "run a node": r"\brun(ning)? (a|your( own)?|an?) (full |light |archive |testnet |validator )?node\b|\bnode (setup|install(ation)?|operator) (guide|instructions|program)\b|\bnode requirements\b|\bhosting a node\b",
    "collateral": r"\bcollateral\b|\bnode (license|sale|key)s?\b|\bstake to run\b",
    "validator": r"\b(run|become|operate|operating|join as|setup|set up) (a |as a )?validator\b|\bnodes? (or|and|&) (a )?validators?\b|\bvalidators? (or|and|&) (a )?nodes?\b|\bvalidator (node|set|setup|guide|requirements|onboarding)\b|\bincentivi[sz]ed testnet\b",
    "worker nodes": r"\b(worker|storage|compute|relay|gateway|sentry|rpc|prover|sequencer) nodes?\b",
}

# Things that look the same but are NOT what we want.
NEG_PATTERNS = {
    "anti-bot/captcha": r"\bcaptcha\b|\banti[\s-]*(spam|bot|abuse|ddos)\b|\bbot (protection|check|challenge)\b|\bbrowser (check|challenge)\b|\bhashcash\b|\bweb ?application firewall\b|\banubis\b|\bproof[\s-]*of[\s-]*work (challenge|captcha)\b",
    "password hashing": r"\bkey derivation\b|\bkdf\b|\bpassword (manager|hashing|vault|generator|hash)\b|\bencrypt(ion|ed)? (file|vault)\b",
    "learning/demo": (r"\btutorial\b|\bhomework\b|\bassignment\b|\bcourse\b|\buniversity\b|\bstudents?\b|\bfor learning\b|"
                      r"\blearning (project|purposes|exercise)\b|\beducational\b|\btoy\b|\bsimple blockchain\b|\bminimal blockchain\b|"
                      r"\bbasic blockchain\b|\bblockchain (demo|simulation|simulator|prototype|example)\b|\bsmall,? readable\b|\bexercise\b|"
                      r"\bbootcamp\b|\bblockchain (in|using|with) (python|javascript|js|java|go|golang|rust|c\+\+|c#|php|node(\.js)?|typescript)\b|"
                      r"\b(build|building|create|creating|implement(ing)?) (a|your own|simple|my own) (simple )?blockchain\b|\bfrom scratch\b|\bproof of concept\b"),
    "portfolio/other": r"\bportfolio\b|\bresume\b|\bcv\b|\bmy (personal )?website\b|\blanding page\b|\bproof[\s-]*of[\s-]*work (page|portfolio|repo(sitory)?)\b|\bstrategic proof of work\b|\bdaily (log|commits?)\b",
    "token/defi": r"\berc[\s-]*20\b|\bbep[\s-]*20\b|\bspl token\b|\bsolidity\b|\bhardhat\b|\bpresale\b|\bairdrop\b|\bmeme ?coin\b|\bpump\.?fun\b|\bnft\b|\bdefi\b|\buniswap\b|\bpancakeswap\b|\bsmart contracts? (for|on) (ethereum|bsc|solana)\b",
    "bot/tool": (r"\btrading bot\b|\btelegram bot\b|\bdiscord bot\b|\bprice (tracker|alert)\b|\bportfolio tracker\b|\bmining calculator\b|"
                 r"\bprofitability\b|\bawesome[\s-]list\b|\bminer (dashboard|monitor|manager)\b|\bmining (rig )?(monitor|dashboard|os)\b|\bhiveos\b|"
                 r"\bcloud mining\b|\bwallet (cracker|drainer|recovery|checker|generator)\b|\bseed phrase\b|\bprivate key (finder|generator|scanner)\b|"
                 r"\bbtc (miner|generator)\b|\bfree (btc|bitcoin|crypto)\b"),
    # HARD: the project IS a tool for some chain (miner, pool software, explorer, genesis generator, wallet app)
    "tool/software": (r"\bminers? (for|app|application|dashboard|software|suite|monitor|manager|tool)\b|\b(cpu|gpu|asic|fpga|cuda|opencl)( (and|or|&) (cpu|gpu))? miner\b|"
                      r"\bminer ?suite\b|\bmining (pool )?(software|engine|stack|suite)\b|\bpool (software|server|engine)\b|\bpool for\b|\bp2pool\b|\bckpool\b|"
                      r"\bminingcore\b|\bcpuminer\b|\bxmrig\b|\bblock ?explorer (for|of)\b|\bexplorer (for|of)\b|\btool for\b|"
                      r"\bgenesis (block )?(generator|blocks? for)\b|\bgenesish0\b|\bwallet (for|app|gui|ui)\b"),
    # SOFT: operations / client tooling words (small penalty only; real chains use them too)
    "ops-tooling": r"\b(masternode|node) (tool|kit|manager|installer|scripts?)\b|\bnode (setup )?guide\b|\b(sdk|library|bindings) for\b|\bdocker (image|compose) for\b|\bhelm chart\b|\bansible\b",
    "mirror/unmodified": r"\bmirror of\b|\bread[\s-]*only mirror\b|\bunofficial mirror\b|\bintegration/staging tree\b",
    "spam": r"\bcasino\b|\bbetting\b|\bslots?\b|\bporn\b|\bkeygen\b|\bcrack(ed)?\b|\bpromo code\b|\bbuy now\b|\bcheap\b.*\b(price|sale)\b|\bseo\b|\bviagra\b",
}
# Hard stops when found in name/description/topics.
HARD_NEG_HEAD = {"anti-bot/captcha", "portfolio/other", "learning/demo", "spam", "bot/tool", "tool/software"}

# --------------------------------------------------------------------------- #
# TREE patterns (file / folder names). Paths inside vendored dependencies are ignored.
# --------------------------------------------------------------------------- #
IGNORED_PATH = re.compile(r"(^|/)(node_modules|vendor|third[_-]?party|depends|contrib|external|deps|\.github|test|tests)(/|$)", re.I)
_ALGO_DIRS = ("randomx|kawpow|ethash|progpow|equihash|yespower|yescrypt|x16r|x16rv2|cryptonight|heavyhash|"
              "kheavyhash|autolykos|cuckatoo|cuckaroo|verthash|ghostrider|firopow|meowpow|fishhash|xelishash")
TREE_POW = {
    "file:pow": r"(^|/)(pow|proofofwork|proof_of_work)\.(cpp|h|rs|go|c|py|js|ts|java|zig)$",
    "file:algo": rf"(^|/)({_ALGO_DIRS})([_-]?\w*)?(/|\.(cpp|h|c|rs|go|py|cu|cl)$)",
    "file:miner": r"(^|/)(miner|mining|stratum|cpuminer|minerd|pool)\w*(\.(cpp|h|rs|go|c|py|js|ts|cu)|/)$|(^|/)(miner|mining|stratum)/",
}
TREE_CHAIN = {
    "file:chainparams": r"(^|/)chainparams\w*\.(cpp|h|rs|go)$|(^|/)chain_?params\.(rs|go|py|json|toml)$|(^|/)params\.(go|rs)$",
    "file:genesis": r"(^|/)genesis\w*\.(json|toml|rs|go|cpp|h|yaml|yml|py)$",
    "file:consensus": r"(^|/)consensus(/|\.(cpp|h|rs|go)$)",
    "file:blockchain": r"(^|/)(blockchain|chain)\.(cpp|h|rs|go)$|(^|/)(blockchain|ledger)/",
    "file:wallet": r"(^|/)wallet/(wallet|walletdb|rpcwallet)\.(cpp|h)$",
}
TREE_NODE = {
    "file:masternode": r"(^|/)masternode\w*\.(cpp|h|rs|go)$|(^|/)governance\w*\.(cpp|h)$|(^|/)masternodes?/",
    "file:validator": r"(^|/)(validators?|staking)(/|\.(rs|go|cpp|h)$)",
    "file:node-docs": r"(^|/)(run[-_]?(a[-_])?node|node[-_]?(setup|guide|operator)|validator[-_]?(guide|setup)|testnet[-_]?(guide|setup|join|faucet)|join[-_]testnet|mining[-_]?guide)\b",
}
# Layout of a Bitcoin-Core-style fork (the usual shape of a brand-new coin)
BITCOIN_LAYOUT_ROOT = {"configure.ac", "src", "autogen.sh", "depends", "contrib", "share", "makefile.am"}

# --------------------------------------------------------------------------- #
# CODE patterns (contents of key files)
# --------------------------------------------------------------------------- #
CODE_POW = {
    "code:pow-params": r"\bnPowTargetSpacing\b|\bnPowTargetTimespan\b|\bpowLimit\b|\bCheckProofOfWork\b|\bGetNextWorkRequired\b|\bfPowAllowMinDifficultyBlocks\b|\bpow_limit\b|\btarget_spacing\b|\bdifficulty_adjustment\b|\bdifficulty_window\b|\bpow_algo\w*\b",
    "code:algo": ALGO_STRONG_RE.pattern,
    "code:mining": r"\bsubmitblock\b|\bgetblocktemplate\b|\bstratum\+tcp\b|\bmine_block\b|\bblock_template\b|\bgenerate(toaddress|block)\b|\brandomx_\w+|\brandomx[-_]rs\b|\bkawpow\b|\bhashimoto\b",
}
CODE_CHAIN = {
    "code:genesis": r"\bhashGenesisBlock\b|\bCreateGenesisBlock\b|\bgenesis_hash\b|\bgenesis[_ ]block\b|\bgenesis_time\b|\"genesis\"|\bgenesis:",
    "code:magic": r"\bpchMessageStart\b|\bmagic_bytes\b|\bnetwork_magic\b|\bMESSAGE_START\b",
    "code:subsidy": r"\bGetBlockSubsidy\b|\bnSubsidyHalvingInterval\b|\bblock_reward\b|\bhalving_interval\b|\binitial_reward\b|\bblock_subsidy\b|\bmax_supply\b|\btotal_supply\b",
    "code:address": r"\bPUBKEY_ADDRESS\b|\bbase58Prefixes\b|\baddress_prefix\b|\bbech32_hrp\b|\bbech32HRP\b",
    "code:ports": r"\bnDefaultPort\b|\bdefault_p2p_port\b|\bp2p_port\b|\brpc_port\b|\bvSeeds\b|\bseed_nodes?\b|\bbootnodes?\b|\bbootstrap_nodes?\b",
    "code:net": r"\bregtest\b|\btestnet\b|\bdevnet\b",
}
CODE_NODE = {
    "code:masternode": r"\bCMasternode\w*|\bmasternode\w*|\bsuper_?node\w*",
    "code:validator": r"\bvalidator_set\b|\bValidatorSet\b|\bslashing\b|\bcometbft\b|\btendermint\b|\bcosmos-sdk\b|\bsubstrate\b|\bpolkadot-sdk\b|\bsp-runtime\b|\bunbonding\b|\bstaking\b",
    "code:p2p": r"\blibp2p\b|\bgossipsub\b|\bkademlia\b|\bpeer_id\b|\bPeerManager\b|\bnode_id\b",
}
# Classic "learn blockchain in 200 lines" code
CODE_TOY = {
    "toy": r"\bclass Blockchain\b|\bdef proof_of_work\b|\bdef mine_block\b|\bfunction proofOfWork\b|\bclass Block\b.*\bindex\b|\bhashlib\.sha256\b|\bCryptoJS\.SHA256\b|\bblock\.nonce\b.*\bdifficulty\b",
}

# --------------------------------------------------------------------------- #
# Stage detection (we want NEW projects: pre-testnet or testnet; mainnet is already live)
# --------------------------------------------------------------------------- #
MAINNET_FACTS = re.compile(
    r"\bmainnet (peers?|seed ?nodes?|rpc|explorer|addresses|address prefix|bootstrap|nodes?|endpoint|chain ?id|genesis hash|block height)\b|"
    r"\bmainnet[_ -]?(deployment|launch|live)\.md\b|\bnodes?: live on explorer\b|\bnodes-live\b|\bnow live on mainnet\b|\blive on mainnet\b|"
    r"\b(official )?(website|explorer|exchange)s?:\s*https?://|\bmainnet-(rpc|seed)\b", re.I)
MAINNET_LIVE = re.compile(
    r"\bmainnet (is |has )?(now )?(live|launched|running|released|active|went live|started)\b|\bmainnet launch(ed)?\b(?![^.\n]{0,40}\b(soon|planned|upcoming|q[1-4]|20\d\d)\b)|"
    r"\bcoinmarketcap\b|\bcoingecko\b|\blisted on\b|\b(buy|trade) (it )?on\b|\bnow trading\b|\bexchange listing\b", re.I)
TESTNET_LIVE = re.compile(
    r"\btestnet (is )?(now )?(live|running|available|open|launched|active|started|v\d)\b|\bjoin (the |our )?(public |incentivi[sz]ed )?(test ?net|devnet)\b|"
    r"\b(public|incentivi[sz]ed|live) test ?net\b|\btest ?net faucet\b|\bfaucet\b|\bdevnet (is )?(live|running)\b|\bconnect to (the )?(test ?net|devnet)\b|"
    r"\bget test ?net\b|\brun(ning)? (a |your )?test ?net node\b|\btestnet (explorer|rpc|seed)\b|\b--testnet\b|\btestnet=1\b", re.I)

# --------------------------------------------------------------------------- #
# Discovery (--discover): GitLab search over name/description/topics for a longer window.
# The hits are then judged by the same deep file reader as everything else.
# --------------------------------------------------------------------------- #
SEARCH_WORDS = [
    "proof of work", "proof-of-work", "pow", "masternode", "randomx", "kawpow", "ethash", "sha256d", "scrypt",
    "equihash", "kheavyhash", "autolykos", "yespower", "genesis block", "block reward", "fair launch",
    "altcoin", "cryptocurrency", "blockchain", "mining pool", "miner", "testnet", "validator", "layer1",
    "chainparams", "node",
]
SEARCH_TOPICS = ["proof-of-work", "pow", "mining", "cryptocurrency", "blockchain", "masternode", "altcoin",
                 "randomx", "testnet", "validator", "bitcoin-fork", "layer1"]

# Proof-of-stake wording: a PoS chain is a NODE project, not a PoW coin (unless PoW words are also present = hybrid).
POS_RE = r"proof[\s-]*of[\s-]*stake|\bpos\b|\bdpos\b|\bbft\b|\bproof[\s-]*of[\s-]*authority\b|\bpoa\b"

# --------------------------------------------------------------------------- #
# Recency: stage events found in releases / tags / recent commits / recent diffs
# --------------------------------------------------------------------------- #
# Keyword polling of recently ACTIVE projects (old repositories that are updating now).
# GitLab search matches name / path / description. Order matters only for logging.
ACTIVE_KEYWORDS = ["testnet", "mainnet", "pre-testnet", "genesis", "pow", "randomx", "masternode", "validator"]

EV_PRETESTNET = re.compile(r"\bpre[\s-]?test[\s-]?net\b", re.I)
EV_TESTNET = re.compile(r"\b(test[\s-]?net|dev[\s-]?net|incentivi[sz]ed|faucet)\b", re.I)
EV_MAINNET = re.compile(r"\bmain[\s-]?net\b", re.I)
EV_LAUNCH = re.compile(r"\b(genesis|launch(ed|es|ing)?|go[\s-]?live|went live)\b", re.I)
EV_V1 = re.compile(r"^\s*v?1\.0\.0(?![\d.])", re.I)
# A commit MESSAGE only counts as a stage event when it also contains an action word (messages are noisy).
EV_ACTION = re.compile(r"\b(launch\w*|live|go[\s-]?live|public|open(ed)?|start(ed)?|add(ed)?|introduc\w*|deploy\w*|enable\w*|"
                       r"init\w*|new|genesis|reset|relaunch\w*|join|release[sd]?|activate\w*|mine[dr]?)\b", re.I)

# Mainnet parameters inside chain files (used to date the mainnet launch from file history).
MAINNET_CODE = re.compile(r"CMainParams|CBaseChainParams::MAIN|strNetworkID\s*=\s*\"main\"|\bmain[\s_-]?net\b|"
                          r"Network::Main\b|NetworkType::Main\b|ChainType::Main\b", re.I)
# Markers in ADDED diff lines of code/config files.
DIFF_GENESIS = re.compile(r"hashGenesisBlock|CreateGenesisBlock|genesis[_ ]?hash|genesis[_ ]?block|genesis[_ ]?time|\"genesis\"|\bgenesis:", re.I)
DIFF_TESTNET = re.compile(r"CTestNetParams|\btest[\s_-]?net\b|\bdev[\s_-]?net\b|\bregtest\b", re.I)
DIFF_SEEDS = re.compile(r"\bvSeeds\b|\bboot[\s_-]?nodes?\b|\bseed[\s_-]?nodes?\b|\bdnsseed\b|\bpersistent_peers\b", re.I)
# Files whose diffs are worth reading.
DIFF_CODE_PATH = re.compile(r"chain_?params|genesis|chain[-_]?spec|test[-_]?net|dev[-_]?net|main[-_]?net|bootnodes?|seeds?\b|"
                            r"(^|/)(config|params|consensus|network|networks|node_config)\.(toml|ya?ml|json|go|rs|py|cpp|h)$", re.I)
DIFF_DOC_PATH = re.compile(r"(^|/)(readme|changelog|release[-_]?notes|roadmap|testnet|mainnet|launch)\w*\.(md|rst|txt)$|"
                           r"(^|/)docs?/[^/]*(testnet|mainnet|node|validator|mining|launch|genesis)[^/]*\.(md|rst|txt)$", re.I)
