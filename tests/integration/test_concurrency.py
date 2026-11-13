
import concurrent.futures
import uuid

import requests


URL = "http://localhost:8001/events"
EVENT_ID = f"CONCURRENT_{uuid.uuid4().hex}"
REQUEST_COUNT = 10

event = {
    "event_id": EVENT_ID,
    "site_id": "Hospital_A",
    "event_type": "patient_registration",
    "base_priority": 2,
    "prerequisite_ids": [],
    "payload": {
        "patient_id": "P_CONCURRENT",
        "name": "Concurrency Test",
    },
}


def submit_event(_):
    try:
        response = requests.post(
            URL,
            json=event,
            timeout=15,
        )

        return {
            "status_code": response.status_code,
            "result": response.json().get("result"),
        }

    except Exception as exc:
        return {
            "status_code": "ERROR",
            "result": str(exc),
        }


def main():
    print(f"Event ID: {EVENT_ID}")
    print(f"Sending {REQUEST_COUNT} concurrent requests...\n")

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=REQUEST_COUNT
    ) as executor:
        results = list(
            executor.map(submit_event, range(REQUEST_COUNT))
        )

    for index, result in enumerate(results, 1):
        print(
            f"Request {index}: "
            f"HTTP {result['status_code']} | "
            f"{result['result']}"
        )

    created = sum(
        r["status_code"] == 201
        and r["result"] == "applied"
        for r in results
    )

    duplicates = sum(
        r["status_code"] == 200
        and r["result"] == "already_applied"
        for r in results
    )

    print("\n--- SUMMARY ---")
    print(f"Created: {created}")
    print(f"Duplicates: {duplicates}")
    print(f"Other results: {REQUEST_COUNT - created - duplicates}")

    assert created == 1, "Expected exactly one new event"
    assert duplicates == 9, "Expected nine duplicate responses"

    print("\nCONCURRENCY TEST PASSED")


if __name__ == "__main__":
    main()
