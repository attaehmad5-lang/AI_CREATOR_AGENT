from creator_brain import CreatorBrain

def main():
    print()
    print("===================================")
    print("       AI CREATOR AGENT")
    print("===================================")
    print()

    topic = input("Enter your content topic: ")

    if not topic.strip():
        print("No topic entered.")
        return

    brain = CreatorBrain()
    brain.run(topic)


if __name__ == "__main__":
    main()
