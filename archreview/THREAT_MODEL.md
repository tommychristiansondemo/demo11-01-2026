# Threat model: Enterprise Solution Review Assistant (demo)

Scope: the deployed demo (CloudFront/S3 site, API Gateway + Lambda, Cognito, DynamoDB, Bedrock, two
AgentCore MCP runtimes). Data is synthetic. User proposals are the only user-supplied content.

## Assets
| Asset | Sensitivity |
|---|---|
| Proposal text submitted by users | Internal (users are told not to enter real data); stored 7 days with TTL |
| Cognito credentials and ID tokens | Account takeover of a demo user |
| AWS credentials and IAM roles | High: never leave AWS; workloads use role credentials only |
| Bedrock spend | Cost abuse |
| Integrity of review output | Misleading advice if citations or verdicts could be forged |

## Trust boundaries
1. Internet → CloudFront (static site) and Internet → API Gateway (JSON API)
2. API Lambda → Worker Lambda (async invoke)
3. Worker → Bedrock (model) and Worker → AgentCore Runtime (MCP, SigV4)
4. Model output → validator → user (the model is treated as untrusted)

## STRIDE analysis
| Threat | Example | Mitigations |
|---|---|---|
| **Spoofing** | Call API as another user | Cognito user-pool authorizer on all data routes; owner check on `GET /reviews/{id}`; governance role from `cognito:groups` claim only |
| | Call MCP runtimes directly | Runtimes require SigV4; only the worker role has `InvokeAgentRuntime` on the two ARNs; endpoints are never sent to the browser |
| **Tampering** | Forge citations or claim fake approvals | Deterministic validator drops any ID not returned by the named server; names and statuses overwritten from retrieved records; `sources` built from the call ledger |
| | Modify site content | S3 Block Public Access + CloudFront origin access control; only the deployment role writes |
| **Repudiation** | Deny submitting a review | Reviews stored with owner, timestamps, and correlation ID; API access logs (JSON), Lambda logs |
| **Information disclosure** | Proposal text in logs | Logger redacts sensitive keys by default (`LOG_PROPOSAL_TEXT=false`); MCP servers log argument sizes only; tested |
| | Internal errors leaked | Generic error bodies with correlation ID; unit-tested |
| | XSS via model or user text | Front end renders with `textContent` only; strict CSP (`script-src 'self'`, no inline); narrative markdown escaped |
| | Cross-origin reads | CORS allow-origin fixed to the site; never reflected |
| **Denial of service / cost** | Flood API or Bedrock | WAF rate limits (CloudFront 1000/5 min/IP; API 300/5 min/IP; `/auth/*` 100/5 min/IP); API Gateway throttling 20 rps; 8 KB body limit; per-user 10 reviews/hour; worker reserved concurrency 10; 6-turn model loop; 230 s deadline; 300 s Lambda timeout; async retries 0 |
| **Elevation of privilege** | Prompt injection makes the agent change verdict or reveal its prompt | Input screen flags instruction-like text and the model is told it is data; retrieved records are scrubbed (redacted) before the model sees them; output validated; tools are read-only, with no side-effecting tools |
| | Over-privileged roles | Per-component IAM roles; MCP runtime roles cannot call Bedrock or other runtimes; worker cannot manage Cognito; API cannot call Bedrock |

## Prompt-injection specifics
- **Direct (user input):** regex screen flags patterns (ignore instructions, system override, forced verdict, tag
  injection, prompt exfiltration, jailbreak). The text is wrapped in `<proposal>` delimiters with tag-like strings
  neutralized, and a security notice is shown to the user.
- **Indirect (retrieved data):** `SVC-LEGACY-FTP.notes` intentionally contains an injection. Any string field matching
  the screen is replaced with `[REDACTED: ...]` before it reaches the model, and the event is reported.
- **Residual risk:** heuristics can miss novel phrasings. The validator still prevents fabricated records, but the model's
  free-text rationale could be skewed. Mitigation: human review (advisory disclaimer), output labeled advisory. A
  Bedrock Guardrail could be added for production.

## Accepted demo risks (would change for production)
- Weak password policy (6 characters, no complexity, no MFA, no email recovery): requested for classroom ease. A WAF
  `/auth/*` rate limit reduces brute force; the data is synthetic.
- Anyone can self-register either role, including governance.
- AWS-managed encryption keys rather than customer-managed KMS keys.
- No Bedrock Guardrail (cost and simplicity); relies on prompt rules, scrubbing, and validation.
- AgentCore runtimes use the PUBLIC network mode (IAM-authenticated) rather than a VPC.
