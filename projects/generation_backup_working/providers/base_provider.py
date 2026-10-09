class BaseProvider:
    name = "BaseProvider"

    def generate_image(self, prompt, output_path):
        raise NotImplementedError("generate_image() must be implemented")
