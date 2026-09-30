FROM public.ecr.aws/e1h7x4a2/plow-cloud-agents@sha256:a7eff7b694a2f7a682f5dd2e5593596fd5fed5229e0410301ea817939383cdd4

ARG SOURCE_REVISION
LABEL org.opencontainers.image.title="RecruiterAgent" \
      org.opencontainers.image.description="Agentic application review with evidence and human curation" \
      org.opencontainers.image.source="https://github.com/synchronic1/RecruiterAgent" \
      org.opencontainers.image.url="https://aiworthusing.com/agent-index/recruiteragent" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.revision=$SOURCE_REVISION

USER root
COPY skills/recruiteragent /opt/plow/skills/recruiteragent
COPY install_runtime.py smoke.py prompt.md /opt/recruiteragent/
RUN python3 /opt/recruiteragent/install_runtime.py
RUN chmod -R a+rX /opt/plow/skills/recruiteragent /opt/recruiteragent

ENV AGENT_ID=recruiteragent \
    AGENT_NAME=RecruiterAgent \
    AGENT_BLURB="Cut through high volumes of applications with agentic AI and evidence-backed review, curated by humans." \
    AGENT_RUNTIME=OpenClaw \
    PATH="/opt/plow/skills/recruiteragent/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

USER node
# Keep Plow's inherited entrypoint, boot command, probe, and usage reporter.
