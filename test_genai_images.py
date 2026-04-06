import asyncio
from google import genai
from google.genai import types

async def main():
    client = genai.Client()
    try:
        response = await client.aio.models.generate_images(
            model='imagen-3.0-generate-002',
            prompt='A cute baby dinosaur',
            config=types.GenerateImagesConfig(
                number_of_images=1,
                aspect_ratio="1:1"
            )
        )
        print([r.image.image_bytes[:10] for r in response.generated_images])
    except AttributeError as e:
        print("AttributeError:", e)
        print("Methods in aio.models:")
        print(dir(client.aio.models))

if __name__ == "__main__":
    asyncio.run(main())
