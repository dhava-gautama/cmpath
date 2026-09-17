"""Run: python examples/task_workflow.py. The example makes no model calls."""
from cmpath import TaskMemory

with TaskMemory() as memory:
    report = memory.create_task("Helios weekly report",aliases=["solar study"],
                                snapshot={"next_action":"draft client invoice"})
    source = memory.append(report.id,"user","The approved budget is 3400 USD.",
                            source={"document":"approval-note","section":"budget"})
    memory.set_fact(report.id,"approved_budget_usd",3400,evidence_id=source.id)
    coffee = memory.create_task("Coffee article")
    memory.append(coffee.id,"assistant","The draft discusses washed and natural beans.")
    memory.resume(coffee.id)

    resolution = memory.resolve("Return to the solar study")
    print("Resolution:",resolution)
    print("Active task before resume:",memory.state()["active_task"])
    if resolution.status == "resolved":
        print("Restored state:",memory.resume(resolution.task_id))
        context = memory.context(resolution.task_id,"What was the approved budget?",
                                 budget=2000,reserve=300)
        print("Evidence IDs:",context.citations)
        print("Estimated input units:",context.used_units)
        print("Model payload:",context.as_messages())
