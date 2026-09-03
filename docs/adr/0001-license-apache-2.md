# ADR 0001: Apache-2.0 License

**Status:** Accepted  
**Date:** 2026-09-03  
**Deciders:** Ashwin Chidambaram (product owner), build orchestrator

## Context

Read The Fine Corpus is a self-hosted infrastructure product targeted at enterprise procurement and deployment. The specification (Open Decision #1) requires a licensing decision that balances openness, commercial viability, and enterprise adoption.

The product is designed to be self-hosted within corporate environments, often alongside regulated and sensitive content. Enterprises acquiring infrastructure software have specific licensing requirements, particularly around patent indemnification and the ability to fork or maintain their own deployments if needed.

## Decision

The entire repository is licensed under **Apache License 2.0** with no portions held back or kept proprietary.

## Rationale

- **Permissive license:** Apache-2.0 imposes minimal restrictions on use, modification, and distribution, making adoption easier for enterprises.
- **Explicit patent grant:** The license includes an explicit patent grant from contributors, addressing a key enterprise procurement requirement around freedom from patent claims.
- **Industry standard for infrastructure:** Apache-2.0 is the standard license for self-hosted and infrastructure software, familiar to enterprise legal and procurement teams.
- **Vendor neutrality:** A permissive license supports the goal of being a boring, inspectable implementation. Enterprises are more likely to trust and extend software they can fully control.
- **Third-party hosting allowed:** The license permits commercial hosting providers to offer managed versions of this software, increasing adoption reach.

## Consequences

- The entire codebase is freely available for use, modification, and redistribution by anyone, including commercial entities.
- No proprietary features or commercial-only lock-in is used to drive adoption. All value must come from the product itself.
- Third parties may offer managed hosting or distribution of this software with their own commercial terms, provided they comply with Apache-2.0.
- Contributors grant all necessary patent licenses for their contributions.

## Alternatives Considered

- **MIT License:** Permissive like Apache-2.0 but lacks an explicit patent grant. Patent claims by contributors remain unclear, which creates procurement friction in regulated industries.
- **AGPL-3.0:** Copyleft license requiring derivatives and network-distributed modifications to be open-sourced. This would deter cloud resellers but also discourages enterprise adoption, as teams are wary of copyleft obligations in their infrastructure layer.
- **Business Source License (BSL) / Source-available:** Proprietary commercial model with code available but use restricted. Not OSI-approved. Creates a business-model dependency at launch rather than focusing on product quality, and is contrary to the product's open infrastructure positioning.
