<!--
Sync Impact Report:
Version change: 1.0.0 → 1.1.0
Modified principles: N/A
Added sections:
  - Core Principles: V. Test-Driven Development (new principle)
  - Development Workflow: Updated Quality Gates to include MCP Playwright validation
Removed sections: N/A
Templates requiring updates:
  ✅ plan-template.md - Constitution Check section is generic and works with any constitution
  ✅ spec-template.md - No changes needed (generic structure)
  ✅ tasks-template.md - No changes needed (generic structure)
Follow-up TODOs: None
-->

# TimeTracker Constitution

## Core Principles

### I. Autonomy & Proactive Execution

Agents and developers MUST operate with minimal permission requests. When a clear path forward exists based on available context, proceed with implementation rather than asking for confirmation. Permission should only be requested when:
- Critical decisions require explicit user input (e.g., breaking changes, security implications)
- Multiple valid approaches exist and the choice significantly impacts architecture
- Required information is genuinely unavailable in the codebase or context

**Rationale**: Reduces friction in development workflow, enables faster iteration, and respects the user's time while maintaining safety through clear exception boundaries.

### II. Planning-First Approach

All non-trivial work MUST begin with a proposed plan before execution. Plans must include:
- Clear breakdown of steps or phases
- Identification of dependencies and risks
- Proposed technical approach with rationale
- Success criteria or validation checkpoints

Plans should be presented for review, but execution may proceed if no objections are raised within a reasonable timeframe (or immediately for low-risk changes).

**Rationale**: Prevents wasted effort on incorrect approaches, enables early feedback, and ensures alignment before significant time investment.

### III. Quality & Scalability Over Speed

Technical decisions MUST prioritize long-term maintainability and scalability over rapid delivery. This means:
- Choose architectures that can grow with the project
- Implement proper abstractions and separation of concerns
- Avoid shortcuts that create technical debt
- Design for extensibility even if current requirements are simple

Speed optimizations are acceptable only when they don't compromise code quality, testability, or future flexibility. When trade-offs exist, document the decision and rationale.

**Rationale**: Reduces long-term maintenance costs, enables easier feature additions, and prevents premature optimization while avoiding architectural shortcuts that become costly later.

### IV. Stability & Documentation Over Novelty

Technology choices MUST favor well-documented, stable, and widely-adopted solutions over cutting-edge alternatives. This includes:
- Prefer mature libraries and frameworks with active maintenance
- Choose technologies with comprehensive documentation and community support
- Avoid experimental or beta features unless they solve a specific problem that mature alternatives cannot
- Prioritize technologies with clear upgrade paths and long-term support

When evaluating new technologies, require evidence of:
- Production usage in similar contexts
- Comprehensive documentation
- Active community or commercial support
- Clear migration path from current stack (if replacing existing technology)

**Rationale**: Reduces risk of adoption issues, ensures team can effectively use and maintain the technology, and provides reliable support channels when problems arise.

### V. Test-Driven Development

All development MUST follow Test-Driven Development (TDD) methodology. This means:
- Tests MUST be written BEFORE implementation code
- Follow the Red-Green-Refactor cycle: Write failing test → Implement minimum code to pass → Refactor
- Tests serve as executable specifications and documentation
- All new features and bug fixes must include corresponding tests

Validation of development MUST be performed using MCP Playwright for end-to-end testing and validation. This includes:
- Automated browser-based testing for user-facing features
- Validation of user workflows and interactions
- Regression testing to ensure existing functionality remains intact
- Integration with the development workflow to validate changes before completion

**Rationale**: TDD ensures code quality, prevents regressions, and provides confidence in refactoring. MCP Playwright provides reliable, automated validation of user-facing functionality, ensuring that development meets user requirements and maintains system integrity.

## Technology Selection Guidelines

When selecting technologies, frameworks, or libraries, apply the following criteria in order of priority:

1. **Documentation Quality**: MUST have comprehensive, up-to-date documentation covering common use cases, API reference, and troubleshooting guides.

2. **Stability & Maturity**: Prefer technologies that have been in production use for at least 1-2 years with a stable release history.

3. **Community & Support**: Should have active community (forums, Stack Overflow presence) or commercial support options.

4. **Maintenance Status**: MUST show recent commits/updates (within last 6 months) or clear maintenance commitment.

5. **Compatibility**: MUST be compatible with existing project dependencies and target platforms without requiring significant workarounds.

Exceptions to these guidelines require explicit justification documenting why the mature alternative is insufficient for the specific use case.

## Development Workflow

### Planning Phase

Before beginning implementation work:

1. **Analyze Requirements**: Understand the full scope of the requested feature or change.

2. **Research & Design**: Investigate technical approaches, evaluate options against principles, and design the solution.

3. **Propose Plan**: Present a clear plan with:
   - Technical approach and rationale
   - Implementation steps or phases
   - Dependencies and potential risks
   - Estimated complexity

4. **Review & Approval**: Wait for explicit approval OR proceed if plan is low-risk and no objections are raised.

### Execution Phase

During implementation:

1. **Follow the Plan**: Execute according to the approved plan, making minor adjustments as needed.

2. **Document Decisions**: Record significant technical decisions and their rationale.

3. **Validate Progress**: Check against success criteria at defined checkpoints.

4. **Communicate Blockers**: Proactively report issues or blockers that require plan adjustments.

### Quality Gates

All work must pass:

- **Constitution Compliance**: Verify alignment with all core principles
- **Code Quality**: Follow established patterns and maintainability standards
- **Documentation**: Update relevant documentation for user-facing or architectural changes
- **Test-Driven Development**: All code must be developed following TDD methodology with tests written before implementation
- **MCP Playwright Validation**: All user-facing features must be validated using MCP Playwright for end-to-end testing

## Governance

This constitution supersedes all other development practices and guidelines. All code, architecture decisions, and development workflows must comply with these principles.

### Amendment Process

1. **Proposal**: Amendments may be proposed by documenting the rationale and impact.

2. **Review**: Proposed changes must be reviewed for:
   - Consistency with existing principles
   - Impact on current and planned work
   - Clarity and testability

3. **Versioning**: Constitution version follows semantic versioning:
   - **MAJOR**: Backward-incompatible changes (principle removals, fundamental redefinitions)
   - **MINOR**: New principles added or existing principles materially expanded
   - **PATCH**: Clarifications, wording improvements, typo fixes

4. **Propagation**: After amendment, all dependent templates and documentation must be updated to reflect changes.

### Compliance Review

- All implementation plans must include a "Constitution Check" section verifying alignment with principles
- Code reviews should verify principle compliance
- Architecture decisions that appear to violate principles require explicit justification in the Complexity Tracking section

**Version**: 1.1.0 | **Ratified**: 2025-12-23 | **Last Amended**: 2025-01-27
